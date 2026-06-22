import os
import json
import re
import subprocess
import requests
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

# =======================================================
# API FUNCTIONS
# =======================================================
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
    print(f"\n[^] Handshaking multipart stream upload for: '{filename}'...")
    try:
        with open(video_path, "rb") as fp:
            # Packing the payload cleanly as a multipart tuple form field
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
    """Removes illegal filesystem characters."""
    return re.sub(r'[\\/*?:"<>|]', "", filename).strip()

# =======================================================
# PIPELINE EXECUTION ENGINE
# =======================================================
def main():
    result = sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=RANGE_NAME).execute()
    rows = result.get("values", [])
    
    if not rows:
        print("Empty sheet found.")
        return

    UPLOAD_SERVER = fetch_upload_server()
    if not UPLOAD_SERVER:
        print("Exiting pipeline: Vidara endpoint lookup failed.")
        return

    for index, row in enumerate(rows[1:], start=2):
        while len(row) < 4:
            row.append("")
            
        title, link, status, error = row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip()
        
        # Skip if already marked success or failed
        if status.lower() in ["success", "failed"]:
            print(f"Row {index}: Skipped ({status})")
            continue
            
        if not link:
            print(f"Row {index}: Skipped due to empty download target URL.")
            continue

        print(f"\n--- Processing Row {index}: {title or 'Untitled'} ---")
        
        # Define clean target filename from Title column
        clean_title = sanitize_filename(title) if title else f"video_{index}"
        final_video_name = f"{clean_title}.mp4"
        
        # Step 1: Downstream Capture via Direct FFMPEG Socket
        print(f"Bypassing Cloudflare wrapper via direct ffmpeg socket...")
        
        ffmpeg_cmd = [
            "ffmpeg",
            "-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36\r\n"
                       "Referer: https://missav.live/\r\n"
                       "Origin: https://missav.live\r\n",
            "-i", link,
            "-c", "copy",          # Stream copy mode (Instant, no re-encoding)
            "-bsf:a", "aac_adtstoasc",
            "-y",                  # Overwrite if exists
            final_video_name
        ]
        
        # Run stream dump and collect output diagnostics
        process = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        
        # Verify if the output file was generated and contains data
        if not (os.path.exists(final_video_name) and os.path.getsize(final_video_name) > 0):
            print(f"Download Engine Failed for Row {index}.")
            # Extract the last few lines of the logs to understand what broke
            log_tail = process.stdout[-500:] if process.stdout else "No log trace available."
            sheet.values().update(
                spreadsheetId=SPREADSHEET_ID, range=f"Sheet1!C{index}:D{index}",
                valueInputOption="RAW", body={"values": [["FAILED", f"ffmpeg capture failed. Log Tail: {log_tail}"]]}
            ).execute()
            continue

        print(f"[✓] Video successfully intercepted via ffmpeg pipeline. Size: {os.path.getsize(final_video_name) / (1024*1024):.2f} MB")

        # Step 2: Vidara Cloud Handoff
        print(f"Uploading file named: '{final_video_name}' to Vidara...")
        upload_result = upload_to_vidara(UPLOAD_SERVER, final_video_name)
        
        if upload_result["success"]:
            print(f"Successfully processed Row {index}! Remote Code: {upload_result.get('filecode')}")
            # Updates Status to SUCCESS, clears out Error cell entirely
            sheet.values().update(
                spreadsheetId=SPREADSHEET_ID, range=f"Sheet1!C{index}:D{index}",
                valueInputOption="RAW", body={"values": [["SUCCESS", ""]]}
            ).execute()
        else:
            print(f"Upload logic failure on Row {index}.")
            # Updates Status to FAILED, writes error details to Error column
            sheet.values().update(
                spreadsheetId=SPREADSHEET_ID, range=f"Sheet1!C{index}:D{index}",
                valueInputOption="RAW", body={"values": [["FAILED", upload_result["error"]]]}
            ).execute()
            
        # Step 3: Clean up local file storage cache
        if os.path.exists(final_video_name):
            try:
                os.remove(final_video_name)
                print("[✓] Local file cache cleared safely.")
            except Exception as ce:
                print(f"[!] Warning: Minor file wipe alert: {ce}")

if __name__ == "__main__":
    main()
