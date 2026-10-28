import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles


DATA_DIR = Path(os.getenv("FITCHECK_DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DATABASE_PATH = DATA_DIR / "fitcheck.sqlite3"
MAX_IMAGE_BYTES = 5 * 1024 * 1024

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    with connect() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS clothing_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                color TEXT NOT NULL,
                season TEXT NOT NULL DEFAULT '',
                occasion TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                image_filename TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize_database()
    yield


app = FastAPI(title="FitCheck API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


def item_from_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    filename = item.pop("image_filename")
    item["image_url"] = f"/uploads/{filename}" if filename else None
    return item


def get_item(item_id: int) -> sqlite3.Row:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM clothing_items WHERE id = ?", (item_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Clothing item not found")
    return row


def detect_image_extension(content: bytes) -> str | None:
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    return None


async def save_image(image: UploadFile | None) -> str | None:
    if image is None or not image.filename:
        return None
    content = await image.read(MAX_IMAGE_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="The selected image is empty")
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Images must be 5 MB or smaller")
    extension = detect_image_extension(content)
    if extension is None:
        raise HTTPException(status_code=400, detail="Upload a valid JPEG, PNG, WebP, or GIF image")
    filename = f"{uuid4().hex}{extension}"
    (UPLOAD_DIR / filename).write_bytes(content)
    return filename


def remove_image(filename: str | None) -> None:
    if filename:
        (UPLOAD_DIR / filename).unlink(missing_ok=True)


def validate_required_fields(name: str, category: str, color: str) -> tuple[str, str, str]:
    name, category, color = name.strip(), category.strip(), color.strip()
    if not name or not category or not color:
        raise HTTPException(status_code=422, detail="Name, category, and color are required")
    return name, category, color


@app.get("/api/items")
def list_items() -> list[dict]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM clothing_items ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return [item_from_row(row) for row in rows]


@app.post("/api/items", status_code=201)
async def create_item(
    name: str = Form(..., min_length=1, max_length=100),
    category: str = Form(..., min_length=1, max_length=40),
    color: str = Form(..., min_length=1, max_length=40),
    season: str = Form("", max_length=40),
    occasion: str = Form("", max_length=80),
    notes: str = Form("", max_length=500),
    image: UploadFile | None = File(None),
) -> dict:
    name, category, color = validate_required_fields(name, category, color)
    filename = await save_image(image)
    try:
        with connect() as connection:
            cursor = connection.execute(
                """INSERT INTO clothing_items
                   (name, category, color, season, occasion, notes, image_filename)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name, category, color, season.strip(), occasion.strip(), notes.strip(), filename),
            )
            row = connection.execute(
                "SELECT * FROM clothing_items WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
    except Exception:
        remove_image(filename)
        raise
    return item_from_row(row)


@app.put("/api/items/{item_id}")
async def update_item(
    item_id: int,
    name: str = Form(..., min_length=1, max_length=100),
    category: str = Form(..., min_length=1, max_length=40),
    color: str = Form(..., min_length=1, max_length=40),
    season: str = Form("", max_length=40),
    occasion: str = Form("", max_length=80),
    notes: str = Form("", max_length=500),
    image: UploadFile | None = File(None),
) -> dict:
    existing = get_item(item_id)
    name, category, color = validate_required_fields(name, category, color)
    new_filename = await save_image(image)
    filename = new_filename or existing["image_filename"]
    try:
        with connect() as connection:
            connection.execute(
                """UPDATE clothing_items
                   SET name = ?, category = ?, color = ?, season = ?, occasion = ?, notes = ?, image_filename = ?
                   WHERE id = ?""",
                (name, category, color, season.strip(), occasion.strip(), notes.strip(), filename, item_id),
            )
            row = connection.execute(
                "SELECT * FROM clothing_items WHERE id = ?", (item_id,)
            ).fetchone()
    except Exception:
        remove_image(new_filename)
        raise
    if new_filename:
        remove_image(existing["image_filename"])
    return item_from_row(row)


@app.delete("/api/items/{item_id}", status_code=204)
def delete_item(item_id: int) -> None:
    existing = get_item(item_id)
    with connect() as connection:
        connection.execute("DELETE FROM clothing_items WHERE id = ?", (item_id,))
    remove_image(existing["image_filename"])
