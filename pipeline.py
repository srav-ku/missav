import os
import json
import subprocess
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# =======================================================
# CONFIGURATION & ENV SETUP
# =======================================================
API_KEY = os.environ.get("VIDARA_API_KEY")
SPREADSHEET_ID = "1HsNADnc31PtDctLE8j8pNag0YA8YIbTg1BjTJ1XxPO4"
RANGE_NAME = "Sheet1!A:D"  # Adjusted assuming sheet title is Sheet1

# Authenticate Google Sheets API
gcp_creds_json = json.loads(os.environ.get("GCP_SERVICE_ACCOUNT"))
creds = Credentials.from_service_account_info(gcp_creds_json, scopes=["https://www.googleapis.com/auth/spreadsheets"])
service = build("sheets", "v4", credentials=creds)
sheet = service.spreadsheets()

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
# API FUNCTIONS
# =======================================================
def fetch_upload_server():
    try:
        response = requests.get("https://api.vidara.so/v1/upload/server", params={"api_key": API_KEY}, timeout=30)
        response.raise_for_status()
        res_json = response.json()
        if res_json.get("status") != 200:
            raise Exception(f"API Error: {res_json.get('message', 'Unknown status')}")
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
            response = requests.post(
                upload_server,
                files={"file": (filename, fp, "video/mp4")},
                data={"api_key": API_KEY},
                timeout=None
            )
        response.raise_for_status()
        data = response.json()
        if "filecode" in data:
            return {"success": True, "remote_url": f"https://vidara.so/{data['filecode']}"}
        elif data.get("result", {}).get("filecode"):
            return {"success": True, "remote_url": f"https://vidara.so/{data['result']['filecode']}"}
        else:
            return {"success": False, "error": f"Invalid frame structure: {data}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

# =======================================================
# PIPELINE EXECUTION ENGINE
# =======================================================
def main():
    # Read the Sheet
    result = sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=RANGE_NAME).execute()
    rows = result.get("values", [])
    
    if not rows:
        print("Empty sheet found.")
        return

    # Extract Header and check row entries
    header = rows[0]
    print(f"Extracted Sheet Header: {header}")

    # Fetch fresh Upload Destination 
    UPLOAD_SERVER = fetch_upload_server()
    if not UPLOAD_SERVER:
        print("Exiting pipeline: Vidara endpoint lookup failed.")
        return

    # Loop rows starting from index 1 (skip header)
    for index, row in enumerate(rows[1:], start=2):
        # Pad columns dynamically to avoid out of bounds exceptions
        while len(row) < 4:
            row.append("")
            
        title, link, status, error = row[0], row[1], row[2], row[3]
        
        # Skip if already marked success or failed
        if status.strip().lower() in ["success", "failed"]:
            print(f"Row {index}: Skipped ({status})")
            continue
            
        if not link:
            print(f"Row {index}: Skipped due to empty download target URL.")
            continue

        print(f"\n--- Processing Row {index}: {title or 'No Title'} ---")
        output_file = f"video_{index}.mp4"
        
        # Step 1: High-Speed Downstream Capture via yt-dlp Subprocess execution
        try:
            cmd = [
                "yt-dlp",
                "--cookies", "cookies.txt",
                "--add-header", "Referer:https://njavtv.com/",
                "--add-header", "Origin:https://njavtv.com",
                "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
                "--newline", "--no-part", "--retries", "10", "--fragment-retries", "10",
                "--concurrent-fragments", "8", "-N", "8",
                "-o", output_file, link
            ]
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as err:
            print(f"Download Engine Failed for Row {index}.")
            sheet.values().update(
                spreadsheetId=SPREADSHEET_ID, range=f"Sheet1!C{index}:D{index}",
                valueInputOption="RAW", body={"values": [["FAILED", f"yt-dlp download failed: {err}"]]}
            ).execute()
            continue

        # Step 2: Verification & Vidara Cloud Handoff
        if os.path.exists(output_file):
            print(f"Downloaded asset verified. Performing API stream upload...")
            upload_result = upload_to_vidara(UPLOAD_SERVER, output_file)
            
            if upload_result["success"]:
                print(f"Successfully processed Row {index}!")
                sheet.values().update(
                    spreadsheetId=SPREADSHEET_ID, range=f"Sheet1!C{index}:D{index}",
                    valueInputOption="RAW", body={"values": [["SUCCESS", upload_result["remote_url"]]]}
                ).execute()
            else:
                print(f"Upload logic failure on Row {index}.")
                sheet.values().update(
                    spreadsheetId=SPREADSHEET_ID, range=f"Sheet1!C{index}:D{index}",
                    valueInputOption="RAW", body={"values": [["FAILED", upload_result["error"]]]}
                ).execute()
                
            # Safely clear cache
            if os.path.exists(output_file):
                os.remove(output_file)
        else:
            sheet.values().update(
                spreadsheetId=SPREADSHEET_ID, range=f"Sheet1!C{index}:D{index}",
                valueInputOption="RAW", body={"values": [["FAILED", "Target artifact missing post compilation execution"]]}
            ).execute()

if __name__ == "__main__":
    main()
