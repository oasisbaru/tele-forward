"""
Telegram (akun sendiri) -> Google Drive.
Dijalankan berkala oleh GitHub Actions. Mendukung file sampai 2 GB.

Catatan: repo public berarti log Actions bisa dilihat siapa saja,
jadi script ini sengaja TIDAK mencetak nama file atau isi chat.
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from telethon import TelegramClient
from telethon.sessions import StringSession

STATE = Path("state.json")
FOLDER_NAME = os.environ.get("DRIVE_FOLDER_NAME") or "Telegram Files"
SOURCE = os.environ.get("SOURCE_CHAT") or "me"  # "me" = Saved Messages
if SOURCE.lstrip("-").isdigit():
    SOURCE = int(SOURCE)


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"last_id": None}


def save_state(state):
    STATE.write_text(json.dumps(state))


def drive_service():
    creds = Credentials(
        None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/drive.file"],
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_folder_id(drive):
    q = (
        f"name='{FOLDER_NAME}' and "
        "mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    res = drive.files().list(q=q, fields="files(id)", spaces="drive").execute()
    if res["files"]:
        return res["files"][0]["id"]
    folder = drive.files().create(
        body={"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"},
        fields="id",
    ).execute()
    return folder["id"]


def already_uploaded(drive, key):
    """Cek apakah pesan Telegram ini sudah pernah diunggah (anti-duplikat)."""
    q = f"appProperties has {{ key='tg' and value='{key}' }} and trashed=false"
    res = drive.files().list(q=q, fields="files(id)", spaces="drive").execute()
    return bool(res["files"])


def upload(drive, folder_id, path, name, key):
    media = MediaFileUpload(path, resumable=True, chunksize=32 * 1024 * 1024)
    req = drive.files().create(
        body={"name": name, "parents": [folder_id], "appProperties": {"tg": key}},
        media_body=media,
        fields="id",
    )
    resp = None
    while resp is None:
        _, resp = req.next_chunk(num_retries=5)
    return resp["id"]


def pick_name(m):
    if m.file.name:
        name = m.file.name
    else:
        kind = "video" if m.video else "foto" if m.photo else "file"
        name = f"{kind}_{m.date:%Y%m%d_%H%M%S}_{m.id}{m.file.ext or ''}"
    return name.replace("/", "_").replace("\\", "_")


async def main():
    client = TelegramClient(
        StringSession(os.environ["TG_SESSION"]),
        int(os.environ["TG_API_ID"]),
        os.environ["TG_API_HASH"],
    )
    await client.start()
    state = load_state()

    # Run pertama: tandai posisi terakhir, jangan unduh riwayat lama.
    if state["last_id"] is None:
        latest = await client.get_messages(SOURCE, limit=1)
        state["last_id"] = latest[0].id if latest else 0
        save_state(state)
        print("Inisialisasi selesai. Hanya file baru setelah ini yang diproses.")
        await client.disconnect()
        return

    messages = [
        m async for m in client.iter_messages(SOURCE, min_id=state["last_id"], reverse=True)
    ]
    todo = [m for m in messages if m.file is not None and not m.sticker]
    print(f"Pesan baru: {len(messages)}, file untuk diproses: {len(todo)}")

    drive = folder_id = None
    failed = False

    for m in messages:
        if m.file is None or m.sticker:
            state["last_id"] = m.id
            save_state(state)
            continue
        try:
            if drive is None:
                drive = drive_service()
                folder_id = get_folder_id(drive)
            name = pick_name(m)
            key = f"{m.chat_id}_{m.id}"
            if already_uploaded(drive, key):
                # Sudah ada di Drive dari run sebelumnya: lewati, jangan unggah ulang.
                state["last_id"] = m.id
                save_state(state)
                print("SKIP: 1 file sudah ada di Drive")
                continue
            with tempfile.TemporaryDirectory() as tmp:
                path = await m.download_media(file=os.path.join(tmp, name))
                upload(drive, folder_id, path, name, key)
            state["last_id"] = m.id
            save_state(state)
            await client.send_message(SOURCE, f"✅ Tersimpan di Drive: {name}", reply_to=m.id)
            print("OK: 1 file tersimpan")
        except Exception as e:
            print(f"GAGAL pada 1 file ({type(e).__name__}). Akan dicoba lagi di run berikutnya.")
            try:
                await client.send_message(SOURCE, f"❌ Gagal menyimpan ({type(e).__name__}). Akan dicoba lagi.", reply_to=m.id)
            except Exception:
                pass
            failed = True
            break

    await client.disconnect()
    if failed:
        sys.exit(1)


asyncio.run(main())
