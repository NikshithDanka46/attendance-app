from flask import Flask, render_template, request, redirect, url_for, session, Response, send_file
import sqlite3
import os
import cv2
import numpy as np
import face_recognition
import base64
from datetime import datetime
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors

app = Flask(__name__)
app.secret_key = "attendance_secret"

DB = "attendance.db"
IMAGE_FOLDER = "static/student_images"

# ---------------- DB CONNECTION ----------------
def get_db():
    conn = sqlite3.connect(DB, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS attendance(
            roll TEXT,
            name TEXT,
            branch TEXT,
            session TEXT,
            date TEXT,
            time TEXT,
            UNIQUE(roll, session, date)
        )
    """)
    return conn

# ---------------- GET ALL SESSIONS ----------------
def get_all_sessions():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT session FROM attendance ORDER BY session")
    sessions = [row[0] for row in cursor.fetchall()]
    conn.close()
    return sessions

# ---------------- ATTENDANCE SUMMARY ----------------
def get_attendance_summary():
    conn = get_db()
    cursor = conn.cursor()
    sessions = get_all_sessions()

    cursor.execute("SELECT session, COUNT(DISTINCT date) FROM attendance GROUP BY session")
    total_classes = dict(cursor.fetchall())

    cursor.execute("SELECT roll, name, branch, session, COUNT(*) FROM attendance GROUP BY roll, session")
    rows = cursor.fetchall()
    conn.close()

    students = {}
    for roll, name, branch, session_name, attended in rows:
        if roll not in students:
            students[roll] = {"name": name, "branch": branch, "sessions": {s: 0 for s in sessions}}
        total = total_classes.get(session_name, 0)
        percent = round((attended / total) * 100, 2) if total else 0
        students[roll]["sessions"][session_name] = percent

    return students

# ---------------- LOGIN ----------------
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form["username"] == "admin" and request.form["password"] == "admin":
            session["user"] = "admin"
            return redirect(url_for("home"))
        return "Invalid Login"
    return render_template("login.html")

# ---------------- HOME PAGE ----------------
@app.route("/home")
def home():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("home.html")

# ---------------- DASHBOARD ----------------
@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect(url_for("login"))

    selected_session = request.args.get("session")

    conn = get_db()
    cursor = conn.cursor()

    total_students = len([
        f for f in os.listdir(IMAGE_FOLDER)
        if f.lower().endswith(('.jpg', '.jpeg', '.png'))
    ])

    if selected_session:
        cursor.execute("""
            SELECT roll, name, branch, session, date, time
            FROM attendance
            WHERE session = ?
            ORDER BY CAST(roll AS INTEGER)
        """, (selected_session,))
    else:
        cursor.execute("""
            SELECT roll, name, branch, session, date, time
            FROM attendance
            ORDER BY CAST(roll AS INTEGER)
        """)

    records = cursor.fetchall()
    conn.close()

    present_today = len(set(r[0] for r in records))
    percentage = round((present_today / total_students) * 100, 2) if total_students else 0

    return render_template(
        "dashboard.html",
        records=records,
        total_students=total_students,
        present_today=present_today,
        percentage=percentage,
        student_summary=get_attendance_summary(),
        sessions=get_all_sessions(),
        selected_session=selected_session
    )

# ---------------- PDFs ----------------
@app.route("/attendance_percentage_pdf")
def attendance_percentage_pdf():
    students = get_attendance_summary()
    sessions = get_all_sessions()

    pdf = SimpleDocTemplate("attendance_percentage.pdf", pagesize=A4)

    header = ["Roll", "Name", "Branch"] + [f"{s} %" for s in sessions]
    data = [header]

    for roll in sorted(students.keys(), key=lambda x: int(x)):
        info = students[roll]
        row = [roll, info["name"], info["branch"]]
        for s in sessions:
            row.append(info["sessions"].get(s, 0))
        data.append(row)

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("GRID", (0,0), (-1,-1), 1, colors.black),
        ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
        ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ("FONT", (0,0), (-1,0), "Helvetica-Bold"),
    ]))

    pdf.build([table])
    return send_file("attendance_percentage.pdf", as_attachment=True)

@app.route("/attendance_records_pdf")
def attendance_records_pdf():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT roll, name, branch, session, date, time
        FROM attendance
        ORDER BY date, roll
    """)
    records = cursor.fetchall()
    conn.close()

    pdf = SimpleDocTemplate("attendance_records.pdf", pagesize=A4)

    data = [["Roll", "Name", "Branch", "Session", "Date", "Time"]]
    for row in records:
        data.append(list(row))

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("GRID", (0,0), (-1,-1), 1, colors.black),
        ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
        ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ("FONT", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 9),
    ]))

    pdf.build([table])
    return send_file("attendance_records.pdf", as_attachment=True)

# ---------------- MARK ATTENDANCE ----------------
current_session_name = ""
last_marked_name = ""

@app.route("/mark_attendance")
def mark_attendance():
    return render_template("select_session.html")

@app.route("/start_attendance", methods=["POST"])
def start_attendance():
    global current_session_name, marked_names, last_marked_name
    session_name = request.form.get("session")
    if not session_name:
        return "Session required"

    current_session_name = session_name.upper()
    marked_names = set()
    last_marked_name = ""

    return render_template("attendance_camera.html", session_name=current_session_name)

# ---------------- FACE ENCODING ----------------
known_encodings = []
student_details = []

for file in os.listdir(IMAGE_FOLDER):
    if file.lower().endswith(('.jpg', '.jpeg', '.png')):
        img_path = os.path.join(IMAGE_FOLDER, file)
        img = cv2.imread(img_path)
        enc = face_recognition.face_encodings(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        if enc:
            known_encodings.append(enc[0])
            parts = os.path.splitext(file)[0].split("_")
            student_details.append({
                "roll": parts[0],
                "name": parts[1].upper(),
                "branch": parts[2].upper()
            })

marked_names = set()

# ---------------- NEW: BROWSER CAMERA PROCESSING ----------------
@app.route("/process_frame", methods=["POST"])
def process_frame():
    global marked_names, current_session_name, last_marked_name

    data = request.json["image"]
    img_bytes = base64.b64decode(data.split(",")[1])
    np_img = np.frombuffer(img_bytes, np.uint8)
    frame = cv2.imdecode(np_img, cv2.IMREAD_COLOR)

    small = cv2.resize(frame, (0,0), fx=0.25, fy=0.25)
    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)

    locations = face_recognition.face_locations(rgb)
    encodings = face_recognition.face_encodings(rgb, locations)

    for enc in encodings:
        matches = face_recognition.compare_faces(known_encodings, enc)
        dist = face_recognition.face_distance(known_encodings, enc)

        if True in matches:
            idx = np.argmin(dist)
            if dist[idx] < 0.5:
                student = student_details[idx]
                name = student["name"]

                if name not in marked_names:
                    marked_names.add(name)
                    last_marked_name = f"Attendance marked for {name} ({current_session_name})"

                    now = datetime.now()
                    conn = get_db()
                    conn.execute("""
                        INSERT OR IGNORE INTO attendance
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        student["roll"], name, student["branch"],
                        current_session_name,
                        now.strftime("%Y-%m-%d"),
                        now.strftime("%H:%M:%S")
                    ))
                    conn.commit()
                    conn.close()

    return {"status": "ok"}

# ---------------- OLD OPENCV STREAM (UNCHANGED) ----------------
def generate_frames():
    cap = cv2.VideoCapture(1, cv2.CAP_DSHOW)
    global marked_names, current_session_name, last_marked_name

    while True:
        ret, frame = cap.read()
        if not ret:
            continue

        ret, buffer = cv2.imencode('.jpg', frame)
        frame_bytes = buffer.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

@app.route("/video_feed")
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

# ---------------- LAST MARKED ----------------
@app.route("/last_marked")
def last_marked():
    return last_marked_name

# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)