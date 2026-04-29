"""
Assignment Evaluator — Canvas Auto Fetch + Groq AI Evaluation
--------------------------------------------------------------
FIXES:
  ✅ Handles multiple files per student (subfolder structure)
  ✅ Strict rubric-based evaluation (no same marks issue)
  ✅ Grades uploaded in Canvas rubric format
  ✅ Faculty can add optional comments per student

SETUP:
1. pip install groq requests pandas python-docx pymupdf pillow
2. Fill in your API keys below
3. Make sure submissions/ folder has student subfolders
   (run download_submissions.py first)
4. Run: python evaluate_assignments-groq.py
"""

import os
import re
import json
import base64
import pandas as pd
import requests
from groq import Groq

# ============================================================
# CONFIGURATION — Fill these in
# ============================================================

GROQ_API_KEY     = "Enter your Groq API key"       # https://console.groq.com
CANVAS_API_TOKEN = "Enter your Canvas API"
CANVAS_URL       = "https://canvas.instructure.com"
COURSE_ID        = "Enter your Canvas course ID"
ASSIGNMENT_ID    = "Enter your Canvas assignment ID"

GROQ_MODEL = "llama-3.1-8b-instant"
#GROQ_MODEL         = "llama-3.3-70b-versatile"
SUBMISSIONS_FOLDER = "submissions"

# Set True if you want to add comments for each student manually
FACULTY_COMMENTS_ENABLED = False


# ============================================================
# STEP 1 — FETCH ASSIGNMENT + RUBRIC FROM CANVAS
# ============================================================

def fetch_assignment_from_canvas():
    headers  = {"Authorization": f"Bearer {CANVAS_API_TOKEN}"}
    url      = f"{CANVAS_URL}/api/v1/courses/{COURSE_ID}/assignments/{ASSIGNMENT_ID}"
    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        print(f"❌ Failed to fetch assignment: {response.text}")
        return None, None, None, None, None

    data = response.json()

    # Clean HTML from question
    question = data.get("description", "No description found.")
    question = re.sub(r'<[^>]+>', ' ', question).strip()
    question = re.sub(r'\s+', ' ', question)

    # Parse rubric criteria
    rubric_raw  = data.get("rubric", [])
    rubric_text = ""
    rubric_criteria = []  # List of {id, description, points, ratings}

    if rubric_raw:
        rubric_text = "Rubric Criteria:\n"
        for criterion in rubric_raw:
            cid     = criterion.get("id", "")
            desc    = criterion.get("description", "")
            points  = criterion.get("points", 0)
            ratings = criterion.get("ratings", [])

            rubric_criteria.append({
                "id":          cid,
                "description": desc,
                "points":      points,
                "ratings":     ratings
            })

            rubric_text += f"\n- {desc} (max {points} pts)\n"
            for r in ratings:
                rubric_text += f"    • {r.get('description','')}: {r.get('points',0)} pts\n"
    else:
        rubric_text = """
Rubric (Default):
- Correctness (40 pts): Factual accuracy
- Completeness (30 pts): All parts answered
- Clarity (20 pts): Well structured
- Creativity (10 pts): Extra effort
"""
        rubric_criteria = [
            {"id": "correctness",  "description": "Correctness",  "points": 40, "ratings": []},
            {"id": "completeness", "description": "Completeness", "points": 30, "ratings": []},
            {"id": "clarity",      "description": "Clarity",      "points": 20, "ratings": []},
            {"id": "creativity",   "description": "Creativity",   "points": 10, "ratings": []},
        ]

    total_points = data.get("points_possible", 100)
    assignment_name = data.get("name", "Assignment")

    print(f"\n✅ Assignment : {assignment_name}")
    print(f"   Due        : {data.get('due_at', 'No due date')}")
    print(f"   Points     : {total_points}")
    print(f"   Rubric     : {'Found ✅' if rubric_raw else 'Not attached — using default ⚠️'}")
    print(f"\n📋 Rubric Criteria:")
    for c in rubric_criteria:
        print(f"   - {c['description']}: {c['points']} pts")

    return question, rubric_text, rubric_criteria, total_points, assignment_name


# ============================================================
# STEP 2 — READ STUDENT FILES (supports multiple files)
# ============================================================

def read_student_files(student_folder_path):
    """Read all files in a student's subfolder and combine content."""
    all_text  = ""
    images    = []
    filenames = []

    for filename in sorted(os.listdir(student_folder_path)):
        filepath = os.path.join(student_folder_path, filename)
        if not os.path.isfile(filepath):
            continue

        ext = os.path.splitext(filename)[1].lower()
        filenames.append(filename)

        if ext == ".txt":
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                all_text += f"\n\n[File: {filename}]\n" + f.read()

        elif ext == ".pdf":
            try:
                import fitz
                doc = fitz.open(filepath)
                text = "".join([page.get_text() for page in doc])
                all_text += f"\n\n[File: {filename}]\n" + text
            except Exception as e:
                all_text += f"\n\n[File: {filename}] Error reading: {e}"

        elif ext in [".docx"]:
            try:
                from docx import Document
                doc  = Document(filepath)
                text = "\n".join([p.text for p in doc.paragraphs])
                all_text += f"\n\n[File: {filename}]\n" + text
            except Exception as e:
                all_text += f"\n\n[File: {filename}] Error reading: {e}"

        elif ext in [".cpp", ".c", ".py", ".java", ".js", ".ts", ".html", ".css", ".sql"]:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                all_text += f"\n\n[Code File: {filename}]\n```\n" + f.read() + "\n```"

        elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
            # Groq doesn't support images — note it
            all_text += f"\n\n[Image file: {filename} — cannot be read by text model]"

        else:
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    all_text += f"\n\n[File: {filename}]\n" + f.read()
            except:
                all_text += f"\n\n[File: {filename}] — unreadable format"

    return all_text.strip(), filenames


# ============================================================
# STEP 3 — EVALUATE WITH GROQ (strict rubric)
# ============================================================

def evaluate_student(client, student_name, content, filenames, question, rubric_text, rubric_criteria, total_points):
    print(f"  ⚡ Evaluating: {student_name} ({len(filenames)} file(s): {', '.join(filenames)})...")

    if not content.strip():
        return build_empty_result(student_name, "No readable content found", rubric_criteria)

    # Build rubric criteria description with continuous scoring range
    criteria_details = ""
    criteria_json    = {}
    for c in rubric_criteria:
        ratings     = c.get("ratings", [])
        max_pts     = c["points"]
        # Find min non-zero rating point for range reference
        rating_pts  = sorted([r.get("points", 0) for r in ratings], reverse=True)
        min_pts     = rating_pts[-1] if rating_pts else 0

        # Build rating bands description
        bands = ""
        for r in ratings:
            bands += f"\n      [{r.get('points',0)} pts] {r.get('description','')}"

        criteria_details += f"""
  Criterion: {c['description']} (max {max_pts} pts)
  Rating bands (for reference only — you must score continuously between 0 and {max_pts}):{bands}
  ⚠️  Do NOT just pick a band value. Score ANY integer from 0 to {max_pts} based on quality.
  Example: if work is between Average and Excellent, give {int((rating_pts[0]+rating_pts[1])/2) if len(rating_pts)>1 else max_pts} pts or nearby value.
"""
        criteria_json[c["description"]] = {
            "score": f"<integer 0 to {max_pts}, NOT limited to band values>",
            "rating": "<closest band label>",
            "comment": "<one specific sentence about THIS student's work on this criterion>"
        }

    system_prompt = f"""You are a strict university professor doing RELATIVE evaluation of student assignments.

CRITICAL SCORING RULES:
1. Score CONTINUOUSLY — any integer from 0 to max, NOT just the fixed band values. Also don't give score in decimal values. Only Whole numbers should be used
2. Fixed band values (Excellent/Average/Need Improvement) are REFERENCE POINTS only
3. A student scoring between two bands should get a score between those band values
4. Every student gets a DIFFERENT score based on their actual submission quality
5. Read the submission carefully — base score ONLY on what is actually present
6. Do NOT give same marks to different students
7. Do NOT default to band values — use the full continuous range

Scoring guidance:
   90-100% of max → Excellent work, very few issues
   75-89%  of max → Good work, minor issues
   50-74%  of max → Average work, some gaps
   30-49%  of max → Below average, significant issues
   0-29%   of max → Poor or missing work

Total marks: {total_points}

Respond ONLY in valid JSON. No markdown. No extra text.
JSON format:
{{
  "criteria": {json.dumps(criteria_json, indent=2)},
  "total": <sum of all criterion scores>,
  "percentage": <total/max * 100 rounded to 1 decimal>,
  "grade": "<A>=85, B>=70, C>=55, D>=40, F<40>",
  "overall_feedback": "<3-4 lines specific to THIS student's submission>",
  "strengths": "<specific strengths in THIS submission>",
  "improvements": "<specific areas THIS student needs to improve>"
}}"""

    user_message = f"""Assignment Question:
{question}

Rubric Criteria (score CONTINUOUSLY within each range, not just band values):
{criteria_details}

Student: {student_name}
Files submitted: {', '.join(filenames)}

--- STUDENT SUBMISSION ---
{content[:12000]}
--- END OF SUBMISSION ---

Now evaluate {student_name}'s submission.
IMPORTANT: Give a UNIQUE continuous score for each criterion based on actual quality.
Do NOT just pick the Excellent/Average/Need Improvement band values."""

    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_message}
            ],
            max_tokens=1500,
            temperature=0.1   # Low temp = more consistent, less random
        )

        raw = response.choices[0].message.content.strip()
        # Clean markdown if present
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        result = json.loads(raw)
        result["student_name"] = student_name
        result["files"]        = ", ".join(filenames)
        return result

    except json.JSONDecodeError as e:
        print(f"    ❌ JSON error for {student_name}: {e}")
        return build_empty_result(student_name, f"JSON parse error: {e}", rubric_criteria)
    except Exception as e:
        print(f"    ❌ Error for {student_name}: {e}")
        return build_empty_result(student_name, str(e), rubric_criteria)


def build_empty_result(student_name, reason, rubric_criteria):
    criteria = {}
    for c in rubric_criteria:
        criteria[c["description"]] = {"score": 0, "rating": "N/A", "comment": reason}
    return {
        "student_name":    student_name,
        "files":           "-",
        "criteria":        criteria,
        "total":           0,
        "percentage":      0,
        "grade":           "F",
        "overall_feedback": reason,
        "strengths":       "-",
        "improvements":    "-",
        "faculty_comment": ""
    }


# ============================================================
# STEP 4 — FACULTY COMMENTS (optional)
# ============================================================

#def collect_faculty_comments(results):
 #   print("\n" + "=" * 55)
  #  print("💬 FACULTY COMMENTS (optional)")
   # print("   Press Enter to skip for any student")
    #print("=" * 55)

    #for result in results:
     #   name  = result["student_name"]
      # total = result.get("total", 0)
       # grade = result.get("grade", "-")
        # comment = input(f"\n  {name} [{total} pts | {grade}] → Add comment: ").strip()
        # result["faculty_comment"] = comment

  #  return results


# ============================================================
# STEP 5 — UPLOAD GRADES TO CANVAS (rubric format)
# ============================================================

def get_canvas_students():
    headers  = {"Authorization": f"Bearer {CANVAS_API_TOKEN}"}
    url      = f"{CANVAS_URL}/api/v1/courses/{COURSE_ID}/students"
    response = requests.get(url, headers=headers)
    return response.json() if response.status_code == 200 else []


def upload_grade_to_canvas(user_id, result, rubric_criteria):
    headers = {"Authorization": f"Bearer {CANVAS_API_TOKEN}"}
    url     = f"{CANVAS_URL}/api/v1/courses/{COURSE_ID}/assignments/{ASSIGNMENT_ID}/submissions/{user_id}"

    # Build rubric assessment in Canvas format
    rubric_assessment = {}
    criteria_results  = result.get("criteria", {})

    for criterion in rubric_criteria:
        cid   = criterion["id"]
        cdesc = criterion["description"]

        if cdesc in criteria_results:
            cr    = criteria_results[cdesc]
            score = cr.get("score", 0)

            # Find matching rating ID from Canvas rubric
            rating_id = None
            for rating in criterion.get("ratings", []):
                if rating.get("points") == score:
                    rating_id = rating.get("id")
                    break

            rubric_assessment[cid] = {
                "points": score,
                "comments": cr.get("comment", "")
            }
            if rating_id:
                rubric_assessment[cid]["rating_id"] = rating_id

    # Build comment text
    comment_parts = []
    comment_parts.append(result.get("overall_feedback", ""))
    if result.get("strengths"):
        comment_parts.append(f"Strengths: {result['strengths']}")
    if result.get("improvements"):
        comment_parts.append(f"Improvements: {result['improvements']}")
    if result.get("faculty_comment"):
        comment_parts.append(f"Faculty Note: {result['faculty_comment']}")

    full_comment = "\n\n".join(filter(None, comment_parts))

    payload = {
        "submission": {
            "posted_grade": str(result.get("total", 0))
        },
        "rubric_assessment": rubric_assessment,
        "comment": {
            "text_comment": full_comment
        }
    }

    response = requests.put(url, headers=headers, json=payload)
    return response.status_code == 200


# ============================================================
# STEP 6 — BUILD CSV
# ============================================================

def build_csv(results, rubric_criteria):
    rows = []
    for r in results:
        row = {
            "Student Name": r["student_name"],
            "Files":        r.get("files", "-")
        }
        # Add rubric criterion scores
        criteria_results = r.get("criteria", {})
        for c in rubric_criteria:
            desc = c["description"]
            cr   = criteria_results.get(desc, {})
            row[f"{desc} (/{c['points']})"] = cr.get("score", 0)
            row[f"{desc} Rating"]           = cr.get("rating", "-")

        row["Total"]           = r.get("total", 0)
        row["Percentage"]      = f"{r.get('percentage', 0)}%"
        row["Grade"]           = r.get("grade", "-")
        row["Overall Feedback"]= r.get("overall_feedback", "-")
        row["Strengths"]       = r.get("strengths", "-")
        row["Improvements"]    = r.get("improvements", "-")
        row["Faculty Comment"] = r.get("faculty_comment", "")
        rows.append(row)

    df = pd.DataFrame(rows)
    return df.sort_values("Total", ascending=False)


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 55)
    print("  📚 Assignment Evaluator — Powered by Groq")
    print(f"  ⚡ Model: {GROQ_MODEL}")
    print("=" * 55)

    # Step 1: Fetch assignment from Canvas
    print("\n🔗 Fetching assignment from Canvas...")
    question, rubric_text, rubric_criteria, total_points, assignment_name = fetch_assignment_from_canvas()
    if not question:
        return

    # Step 2: Get student subfolders OR flat files
    if not os.path.exists(SUBMISSIONS_FOLDER):
        print(f"\n❌ submissions/ folder not found! Run download_submissions.py first.")
        return

    all_items = os.listdir(SUBMISSIONS_FOLDER)

    # Detect structure
    has_subfolders = any(os.path.isdir(os.path.join(SUBMISSIONS_FOLDER, f)) for f in all_items)
    has_flat_files = any(os.path.isfile(os.path.join(SUBMISSIONS_FOLDER, f)) for f in all_items)

    student_map = {}  # {student_name: folder_path}

    if has_subfolders:
        print(f"\n📂 Detected subfolder structure")
        for f in sorted(all_items):
            if os.path.isdir(os.path.join(SUBMISSIONS_FOLDER, f)):
                student_name = f.replace("_", " ").strip()
                student_map[student_name] = os.path.join(SUBMISSIONS_FOLDER, f)

    elif has_flat_files:
        import shutil, tempfile
        print(f"\n📂 Detected flat files — grouping by student name...")
        grouped = {}
        for filename in sorted(all_items):
            filepath = os.path.join(SUBMISSIONS_FOLDER, filename)
            if not os.path.isfile(filepath):
                continue
            base = os.path.splitext(filename)[0]
            base = re.sub(r'_\d+$', '', base)
            sname = base.replace("_", " ").strip()
            grouped.setdefault(sname, []).append(filepath)

        temp_base = "submissions_grouped_temp"
        os.makedirs(temp_base, exist_ok=True)
        for sname, files in grouped.items():
            sdir = os.path.join(temp_base, sname.replace(" ", "_"))
            os.makedirs(sdir, exist_ok=True)
            for src in files:
                shutil.copy2(src, os.path.join(sdir, os.path.basename(src)))
            student_map[sname] = sdir

    if not student_map:
        print(f"\n❌ No student submissions found in submissions/")
        return

    print(f"👥 Found {len(student_map)} students")

    # Step 3: Get Canvas students for matching
    canvas_students = get_canvas_students()

    # Step 4: Evaluate each student
    client  = Groq(api_key=GROQ_API_KEY)
    results = []

    print("\n" + "=" * 55)
    print("⚡ Evaluating submissions...")
    print("=" * 55)

    for student_name, folder_path in student_map.items():
        content, filenames = read_student_files(folder_path)

        if not filenames:
            print(f"  ⚠️  {student_name}: No files found")
            continue

        result = evaluate_student(
            client, student_name, content, filenames,
            question, rubric_text, rubric_criteria, total_points
        )
        results.append(result)

    if not results:
        print("❌ No results generated!")
        return

    # Step 5: Faculty comments (optional)
    if FACULTY_COMMENTS_ENABLED:
        results = collect_faculty_comments(results)
    else:
        for r in results:
            r["faculty_comment"] = ""

    # Step 6: Save CSV
    print("\n" + "=" * 55)
    print("📊 Generating results CSV...")
    df = build_csv(results, rubric_criteria)
    csv_filename = f"{assignment_name.replace(' ', '_')}_results.csv"
    df.to_csv(csv_filename, index=False)
    print(f"✅ CSV saved: {csv_filename}")

    # Print summary
    print("\n📋 RESULTS SUMMARY")
    print("-" * 55)
    summary_cols = ["Student Name", "Total", "Grade"]
    print(df[summary_cols].to_string(index=False))
    print(f"\n📈 Class Average : {df['Total'].mean():.1f}/{total_points}")
    print(f"🏆 Highest Score : {df['Total'].max()}/{total_points}")
    print(f"📉 Lowest Score  : {df['Total'].min()}/{total_points}")

    # Step 7: Upload to Canvas
    print("\n" + "=" * 55)
    print("📤 Uploading grades to Canvas (with rubric)...")
    print("=" * 55)

    for result in results:
        matched_id = None
        for s in canvas_students:
            if result["student_name"].lower() in s.get("name", "").lower():
                matched_id = s["id"]
                break

        if matched_id:
            success = upload_grade_to_canvas(matched_id, result, rubric_criteria)
            status  = "✅" if success else "❌"
            print(f"  {status} {result['student_name']}: {result['total']}/{total_points} ({result['grade']})")
        else:
            print(f"  ⚠️  {result['student_name']}: Not matched in Canvas")

    print("\n" + "=" * 55)
    print("🎉 All Done!")
    print(f"📄 Results: {csv_filename}")
    print("=" * 55)


if __name__ == "__main__":
    main()
