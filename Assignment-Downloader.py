"""
Canvas Assignment Submission Downloader
----------------------------------------
- Downloads ALL submissions in parallel (fast!)
- Zips them into submissions.zip
- Extracts into submissions/ folder
- Auto-runs the evaluator script

SETUP:
1. pip install requests
2. Fill in your Canvas details below
3. Run: python download_submissions.py
"""

import os
import re
import sys
import zipfile
import requests
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# CONFIGURATION — Fill these in
# ============================================================

CANVAS_API_TOKEN = "Enter your Canvas API"
CANVAS_URL       = "https://canvas.instructure.com"
COURSE_ID        = "Enter your Canvas course ID"
ASSIGNMENT_ID    = "Enter your Canvas assignment ID"

SUBMISSIONS_FOLDER = "submissions"

ZIP_FILE           = "submissions.zip"
#EVALUATOR_SCRIPT   = "evaluate_assignments-groq.py"

MAX_WORKERS        = 10   # Number of parallel downloads


# ============================================================
# FETCH ALL SUBMISSIONS FROM CANVAS
# ============================================================

def fetch_submissions():
    headers = {"Authorization": f"Bearer {CANVAS_API_TOKEN}"}
    url     = f"{CANVAS_URL}/api/v1/courses/{COURSE_ID}/assignments/{ASSIGNMENT_ID}/submissions"
    params  = {
        "include[]": ["attachments", "user"],
        "per_page": 100
    }
    response = requests.get(url, headers=headers, params=params)
    if response.status_code != 200:
        print(f"❌ Failed to fetch submissions: {response.text}")
        sys.exit(1)
    return response.json()


# ============================================================
# PREPARE DOWNLOAD TASKS
# ============================================================

def prepare_tasks(submissions):
    tasks    = []
    no_files = []

    for submission in submissions:
        user         = submission.get("user", {})
        student_name = user.get("name", f"Student_{submission.get('user_id', 'unknown')}")
        name_clean   = student_name.strip().replace(" ", "_").replace("/", "-")

        sub_type    = submission.get("submission_type")
        attachments = submission.get("attachments", [])

        if not sub_type or sub_type == "none":
            no_files.append(student_name)
            continue

        if attachments:
            for i, att in enumerate(attachments):
                file_url  = att.get("url")
                file_name = att.get("filename", "file")
                ext       = os.path.splitext(file_name)[1]
                # URL decode extension
                ext = requests.utils.unquote(ext)
                save_name = f"{name_clean}_{i+1}{ext}" if len(attachments) > 1 else f"{name_clean}{ext}"
                tasks.append({
                    "type":         "file",
                    "student_name": student_name,
                    "url":          file_url,
                    "save_name":    save_name
                })

        elif sub_type == "online_text_entry":
            body       = submission.get("body", "")
            clean_text = re.sub(r'<[^>]+>', ' ', body).strip()
            clean_text = re.sub(r'\s+', ' ', clean_text)
            tasks.append({
                "type":         "text",
                "student_name": student_name,
                "content":      f"Student: {student_name}\n{'='*40}\n{clean_text}",
                "save_name":    f"{name_clean}.txt"
            })

        elif sub_type == "online_url":
            url_sub = submission.get("url", "")
            tasks.append({
                "type":         "text",
                "student_name": student_name,
                "content":      f"Student: {student_name}\n{'='*40}\nSubmitted URL: {url_sub}",
                "save_name":    f"{name_clean}.txt"
            })

    return tasks, no_files


# ============================================================
# DOWNLOAD A SINGLE FILE
# ============================================================

def download_task(task, temp_dir, headers):
    save_path = os.path.join(temp_dir, task["save_name"])

    try:
        if task["type"] == "file":
            response = requests.get(task["url"], headers=headers, timeout=30)
            if response.status_code == 200:
                with open(save_path, "wb") as f:
                    f.write(response.content)
                size_kb = len(response.content) / 1024
                return ("ok", task["student_name"], task["save_name"], size_kb)
            else:
                return ("fail", task["student_name"], task["save_name"], 0)

        elif task["type"] == "text":
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(task["content"])
            return ("ok", task["student_name"], task["save_name"], 0)

    except Exception as e:
        return ("error", task["student_name"], str(e), 0)


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 55)
    print("  📥 Canvas Submission Downloader")
    print("=" * 55)

    headers = {"Authorization": f"Bearer {CANVAS_API_TOKEN}"}

    # Step 1: Fetch submissions
    print("\n🔗 Fetching submissions from Canvas...")
    submissions = fetch_submissions()
    print(f"✅ Found {len(submissions)} submissions")

    # Step 2: Prepare tasks
    tasks, no_files = prepare_tasks(submissions)
    print(f"📦 Files to download : {len(tasks)}")
    print(f"⚠️  No submissions   : {len(no_files)}")

    if no_files:
        print(f"\n⚠️  Students with no submission:")
        for name in no_files:
            print(f"   - {name}")

    if not tasks:
        print("\n❌ No files to download!")
        sys.exit(1)

    # Step 3: Parallel download into temp folder
    temp_dir = "submissions_temp"
    os.makedirs(temp_dir, exist_ok=True)

    print(f"\n⚡ Downloading {len(tasks)} files in parallel ({MAX_WORKERS} workers)...")
    print("=" * 55)

    downloaded = 0
    failed     = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(download_task, task, temp_dir, headers): task
            for task in tasks
        }
        for future in as_completed(futures):
            status, student, name, size = future.result()
            if status == "ok":
                size_str = f"({size:.1f} KB)" if size > 0 else "(text)"
                print(f"  ✅ {student}: {name} {size_str}")
                downloaded += 1
            else:
                print(f"  ❌ {student}: Failed — {name}")
                failed += 1

    # Step 4: Zip all downloaded files
    print(f"\n📦 Creating {ZIP_FILE}...")
    with zipfile.ZipFile(ZIP_FILE, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename in os.listdir(temp_dir):
            filepath = os.path.join(temp_dir, filename)
            zf.write(filepath, filename)
            
    zip_size = os.path.getsize(ZIP_FILE) / (1024 * 1024)
    print(f"✅ Zip created: {ZIP_FILE} ({zip_size:.1f} MB)")

    # Step 5: Extract into submissions folder
    print(f"\n📂 Extracting to {SUBMISSIONS_FOLDER}/...")
    os.makedirs(SUBMISSIONS_FOLDER, exist_ok=True)
    with zipfile.ZipFile(ZIP_FILE, "r") as zf:
        zf.extractall(SUBMISSIONS_FOLDER)
    print(f"✅ Extracted {downloaded} files to {SUBMISSIONS_FOLDER}/")

    # Step 6: Cleanup temp folder
    import shutil
    shutil.rmtree(temp_dir)
    print(f"🧹 Cleaned up temp folder")

    # Step 7: Summary
    print("\n" + "=" * 55)
    print("📊 DOWNLOAD SUMMARY")
    print("-" * 55)
    print(f"  ✅ Downloaded  : {downloaded}")
    print(f"  ❌ Failed      : {failed}")
    print(f"  ⚠️  No file    : {len(no_files)}")
    print(f"  📦 Zip file    : {ZIP_FILE} ({zip_size:.1f} MB)")
    print(f"  📁 Extracted to: {SUBMISSIONS_FOLDER}/")
    print("=" * 55)

    # Step 8: Auto-run evaluator
    #if downloaded > 0:
     #   if os.path.exists(EVALUATOR_SCRIPT):
      #      print(f"\n🚀 Auto-running evaluator: {EVALUATOR_SCRIPT}")
       #     print("=" * 55)
        #    subprocess.run([sys.executable, EVALUATOR_SCRIPT])
        #else:
         #   print(f"\n✅ Done! Now run:")
          #  print(f"   python {EVALUATOR_SCRIPT}")
    #else:
     #   print("\n⚠️  No files downloaded. Check your ASSIGNMENT_ID and API token.")i


if __name__ == "__main__":
    main()
