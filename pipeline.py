import os
import json
import re
import subprocess
import requests
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# =======================================================
# CONFIGURATION & ENV SETUP
# =======================================================
API_KEY = os.environ.get("VIDARA_API_KEY")
SPREADSHEET_ID = "1HsNADnc31PtDctLE8j8pNag0YA8YIbTg1BjTJ1XxPO4"
RANGE_NAME = "Sheet1!A:D"  # Columns: A=Title, B=Link, C=Status, D=Error

# Authenticate Google Sheets API
gcp_creds_json = json.loads(os.environ.get("GCP_SERVICE_ACCOUNT"))
creds = Credentials.from_service_account_info(gcp_creds_json, scopes=["https://www.googleapis.com/auth/spreadsheets"])
service = build("sheets", "v4", credentials=creds)
sheet = service.spreadsheets()

# Thread lock to keep Google Sheets API updates sequential and safe
sheets_lock = Lock()

# Write cookies file
cookies_content = r'''# Netscape HTTP Cookie File
# https://curl.haxx.se/rfc/cookie_spec.html

njavtv.com  FALSE / FALSE 1794810815  user_uuid YOUR_COOKIE
njavtv.com  FALSE / TRUE  1779266015  XSRF-TOKEN  YOUR_COOKIE
njavtv.com  FALSE / TRUE  1779266015  missav_session  YOUR_COOKIE
.njavtv.com TRUE  / TRUE  1794810816  cf_clearance  YOUR_COOKIE
'''
with open("cookies.txt", "w", encoding="utf-8") as f:
    f.write(cookies_content)

# =======================================================
# API & GOOGLE SHEETS UTILITIES
# =======================================================
def update_sheet_status(index, status, error_msg=""):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with sheets_lock:  # Prevent threads from overlapping API calls
                sheet.values().update(
                    spreadsheetId=SPREADSHEET_ID, 
                    range=f"Sheet1!C{index}:D{index}",
                    valueInputOption="RAW", 
                    body={"values": [[status, error_msg]]}
                ).execute()
            print(f"[✓] Row {index} Updated -> Status: {status} | Error: '{error_msg}'")
            return True
        except Exception as e:
            print(f"[!] Sheets Update Attempt {attempt + 1} failed ({e}). Retrying...")
            time.sleep(2)
            
    print(f"[CRITICAL] Total failure writing to Sheet for Row {index}.")
    return False

def fetch_upload_server():
    try:
        response = requests.get("https://api.vidara.so/v1/upload/server", params={"api_key": API_KEY}, timeout=30)
        response.raise_for_status()
        res_json = response.json()
        if res_json.get("status") != 200:
            raise Exception(f"API Server Error: {res_json.get('message', 'Unknown status')}")
        return res_json["result"]["upload_server"]
    except Exception as e:
        print(f"Error getting server: {e}")
        return None

def upload_to_vidara(upload_server, video_path):
    if not os.path.exists(video_path):
        return {"success": False, "error": "Local file target not found."}
    
    filename = os.path.basename(video_path)
    try:
        with open(video_path, "rb") as fp:
            payload = {
                "api_key": (None, API_KEY),
                "file": (filename, fp, "video/mp4")
            }
            response = requests.post(
                upload_server,
                files=payload,
                timeout=None  # Stay open for big transfers
            )
        response.raise_for_status()
        data = response.json()
        
        if "filecode" in data:
            return {"success": True, "filecode": data["filecode"]}
        elif data.get("result", {}).get("filecode"):
            return {"success": True, "filecode": data["result"]["filecode"]}
        else:
            return {"success": False, "error": f"Invalid server response frame: {data}"}
    except requests.exceptions.HTTPError as http_err:
        return {"success": False, "error": f"Vidara rejected payload ({response.status_code}): {response.text}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def sanitize_filename(filename):
    return re.sub(r'[\\/*?:"<>|]', "", filename).strip()

# =======================================================
# WORKER PIPELINE FOR A SINGLE ROW
# =======================================================
def process_row(row_data, upload_server):
    index, row = row_data
    while len(row) < 4:
        row.append("")
        
    title, link, status, error = row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip()
    
    if status.lower() in ["success", "failed"] or not link:
        return

    print(f"\n--- [Thread Start] Processing Row {index}: {title or 'Untitled'} ---")
    
    clean_title = sanitize_filename(title) if title else f"video_{index}"
    final_video_name = f"{clean_title}.mp4"
    
    # Step 1: Video Capture Engine
    ffmpeg_cmd = [
        "ffmpeg",
        "-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36\r\n"
                   "Referer: https://missav.live/\r\n"
                   "Origin: https://missav.live\r\n",
        "-i", link,
        "-c", "copy",          # Stream copying (very fast, no re-encoding)
        "-bsf:a", "aac_adtstoasc",
        "-y",                  
        final_video_name
    ]
    
    process = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    
    if not (os.path.exists(final_video_name) and os.path.getsize(final_video_name) > 0):
        print(f"Download Engine Failed for Row {index}.")
        log_tail = process.stdout[-200:] if process.stdout else "No log trace available."
        update_sheet_status(index, "FAILED", f"ffmpeg capture failed: {log_tail}")
        return

    print(f"[✓] Row {index} video downloaded. Size: {os.path.getsize(final_video_name) / (1024*1024):.2f} MB")

    # Step 2: Vidara Cloud Upload
    print(f"Uploading file for Row {index} to Vidara...")
    upload_result = upload_to_vidara(upload_server, final_video_name)
    
    # Step 3: Parse Handoff Output
    if upload_result["success"]:
        print(f"Successfully processed Row {index}!")
        update_sheet_status(index, "SUCCESS", "")
    else:
        print(f"Upload logic failure on Row {index}.")
        update_sheet_status(index, "FAILED", upload_result["error"])
        
    # Step 4: Local Storage Cleanup
    if os.path.exists(final_video_name):
        try:
            os.remove(final_video_name)
            print(f"[✓] Row {index} local cache cleared.")
        except Exception as ce:
            print(f"[!] Warning: Row {index} cache wipe alert: {ce}")

# =======================================================
# PIPELINE EXECUTION ENGINE
# =======================================================
def main():
    try:
        result = sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=RANGE_NAME).execute()
        rows = result.get("values", [])
    except Exception as e:
        print(f"Failed to read initial sheet data: {e}")
        return
    
    if not rows:
        print("Empty sheet found.")
        return

    UPLOAD_SERVER = fetch_upload_server()
    if not UPLOAD_SERVER:
        print("Exiting pipeline: Vidara endpoint lookup failed.")
        return

    # Filter rows that need processing to avoid spawning empty worker threads
    valid_rows = []
    for index, row in enumerate(rows[1:], start=2):
        while len(row) < 4: row.append("")
        if row[2].strip().lower() not in ["success", "failed"] and row[1].strip():
            valid_rows.append((index, row))

    # Process up to 3 videos concurrently. 
    # (3 is a sweet spot for GitHub Actions network bandwidth vs disk limits)
    max_workers = min(3, len(valid_rows)) if valid_rows else 1
    
    print(f"Starting Multi-threaded Execution Engine. Spawning {max_workers} worker threads...")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        executor.map(lambda r: process_row(r, UPLOAD_SERVER), valid_rows)

if __name__ == "__main__":
    main()
