from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import time
import threading
import uuid
from datetime import datetime, timezone
import random

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'
CORS(app)

active_jobs = {}
jobs_lock = threading.Lock()  # FIX #4: Thread-safe dict access

# ─── Cleanup old completed jobs (FIX #3: Memory Leak) ───────────────────────
def cleanup_old_jobs():
    while True:
        time.sleep(300)  # Every 5 minutes
        with jobs_lock:
            to_delete = [
                jid for jid, job in active_jobs.items()
                if job.status in ("completed", "stopped", "error")
                and (datetime.now() - job.start_time).total_seconds() > 600  # 10 min old
            ]
            for jid in to_delete:
                del active_jobs[jid]
                print(f"[Cleanup] Removed old job {jid}")

cleanup_thread = threading.Thread(target=cleanup_old_jobs, daemon=True)
cleanup_thread.start()


class SmartResultChecker:
    def __init__(self, roll, cls, year, job_id):
        self.roll = roll
        self.cls = cls
        self.year = year
        self.job_id = job_id
        # Statuses: pre_waiting → waiting_for_result → checking → completed/stopped/error
        self.status = "pre_waiting"
        self.attempts = 0
        self.result = None
        self.error = None
        self.start_time = datetime.now()
        self.is_running = True
        self.phase_message = ""

    # ── FIX #5: Don't hardcode route.php, discover form action dynamically ──
    def get_form_action(self, soup, base_url):
        form = soup.find("form")
        if form and form.get("action"):
            action = form["action"].strip()
            if action.startswith("http"):
                return action
            return base_url.rstrip("/") + "/" + action.lstrip("/")
        return base_url + "route.php"  # Fallback only

    def is_result_available_for_year(self):
        """Check if result for this year is uploaded on website."""
        try:
            url = "https://bisesahiwal.edu.pk/allresult/"
            response = requests.get(url, timeout=10)
            soup = BeautifulSoup(response.text, "html.parser")

            year_select = soup.find("select", {"name": "year"})
            if year_select:
                for option in year_select.find_all("option"):
                    if self.year in option.get_text(strip=True):
                        print(f"[{self.job_id}] ✅ Year {self.year} found in dropdown!")
                        return True

            if self.year in response.text:
                print(f"[{self.job_id}] ✅ Year {self.year} found in page content!")
                return True

            print(f"[{self.job_id}] ⏳ Year {self.year} not available yet.")
            return False

        except Exception as e:
            print(f"[{self.job_id}] Error checking year availability: {e}")
            return False

    # FIX #2: Robust marks parsing (handles dashes, spaces, empty strings)
    def safe_int(self, val):
        if not val:
            return 0
        val = val.strip().replace("-", "").replace("–", "").replace(" ", "")
        return int(val) if val.isdigit() else 0

    def _make_session(self):
        """Persistent session with connection pooling for speed."""
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=5,
            pool_maxsize=10,
            max_retries=0
        )
        session.mount("https://", adapter)
        session.mount("http://",  adapter)
        return session

    def check_single_time(self, session=None):
        """Single attempt to fetch result. Reuses session if passed."""
        try:
            url = "https://bisesahiwal.edu.pk/allresult/"
            s   = session or self._make_session()

            res  = s.get(url, timeout=8)
            soup = BeautifulSoup(res.text, "html.parser")

            token_input = (
                soup.find("input", {"name": "csrf_token"})
                or soup.find("input", {"name": "_token"})
                or soup.find("input", {"name": "token"})
                or soup.find("input", attrs={"type": "hidden"})
            )
            token = token_input.get("value", "") if token_input else ""

            form_action = self.get_form_action(soup, url)

            data = {
                "class":      "1" if self.cls == "9th" else "2",
                "year":       self.year,
                "sess":       "1",
                "rno":        self.roll,
                "csrf_token": token,
                "commit":     "GET RESULT"
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer":    url
            }

            res2  = s.post(form_action, data=data, headers=headers, timeout=8)
            if res2.status_code != 200:
                return None

            soup2      = BeautifulSoup(res2.text, "html.parser")
            results    = []
            total_marks = None

            for row in soup2.find_all("tr"):
                cols = [c.get_text(strip=True) for c in row.find_all("td")]
                if len(cols) < 4:
                    continue
                subject = cols[1].upper() if len(cols) > 1 else ""
                if not subject:
                    continue

                if self.cls == "9th":
                    marks = self.safe_int(cols[3]) if len(cols) > 3 else 0
                    if marks > 0:
                        results.append({"subject": subject, "marks": marks})
                else:
                    marks9    = self.safe_int(cols[3]) if len(cols) > 3 else 0
                    marks10   = self.safe_int(cols[4]) if len(cols) > 4 else 0
                    practical = self.safe_int(cols[5]) if len(cols) > 5 else 0
                    total     = marks9 + marks10 + practical
                    if total > 0:
                        results.append({
                            "subject": subject, "total": total,
                            "class9": marks9, "class10": marks10,
                            "practical": practical
                        })

                if "TOTAL" in cols[0].upper():
                    total_marks = cols[-1]

            if results:
                return {
                    "success":  True,
                    "results":  results,
                    "total":    total_marks,
                    "attempts": self.attempts
                }
            return None

        except Exception as e:
            print(f"[{self.job_id}] Attempt failed: {str(e)}")
            return None

    def _worker_thread(self, worker_id, found_event):
        """
        One of 3 parallel workers.
        Each has its own TCP session — no shared bottleneck.
        Checks every 0.3-0.5s → combined ~360-600 req/min total.
        First worker to find result sets found_event and stops all workers.
        """
        session = self._make_session()
        print(f"[{self.job_id}] Worker-{worker_id} started")

        while self.is_running and not found_event.is_set():
            with self.attempts_lock:
                self.attempts += 1
                attempt_num = self.attempts

            print(f"[{self.job_id}] W{worker_id} attempt #{attempt_num}")
            result = self.check_single_time(session=session)

            if result:
                if not found_event.is_set():
                    found_event.set()
                    self.result = result
                    self.status = "completed"
                    self.phase_message = f"Result found after {attempt_num} total attempts!"
                    print(f"[{self.job_id}] 🎉 Worker-{worker_id} FOUND RESULT!")
                break

            # Ultra fast: 0.3–0.5s per worker → 3 workers = ~360-600 req/min
            time.sleep(random.uniform(0.3, 0.5))

    def start_smart_checking(self):
        """
        Phase 1 – Poll every 30s until year appears.
        Phase 2 – Launch 3 parallel workers, each checking every 0.3-0.5s.
        """
        self.attempts_lock = threading.Lock()

        # ── PHASE 1 ──────────────────────────────────────────────────────────
        self.status = "waiting_for_result"
        self.phase_message = f"Waiting for {self.year} result to appear on BISE website..."
        print(f"[{self.job_id}] 📡 Phase 1 — polling every 30s")

        while self.is_running and self.status == "waiting_for_result":
            if self.is_result_available_for_year():
                self.status = "checking"
                self.phase_message = "Result uploaded! 3 parallel workers launched!"
                print(f"[{self.job_id}] 🚀 Phase 2 — launching 3 parallel workers!")
                break
            time.sleep(30)

        if not self.is_running:
            self.status = "stopped"
            return

        # ── PHASE 2: 3 parallel workers ──────────────────────────────────────
        NUM_WORKERS = 3
        found_event = threading.Event()

        for i in range(NUM_WORKERS):
            t = threading.Thread(
                target=self._worker_thread,
                args=(i + 1, found_event),
                daemon=True
            )
            t.start()
            time.sleep(0.1)  # Slight stagger so workers don't all hit at same ms

        # Block until result found or stopped
        while self.is_running and not found_event.is_set():
            time.sleep(0.2)

        if not found_event.is_set():
            self.status = "stopped"


# ═══════════════════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def home():
    return render_template("index.html")


@app.route('/start-auto-check', methods=['POST'])
def start_auto_check():
    try:
        data = request.get_json()
        roll = data.get('roll', '').strip()
        cls  = data.get('class', '')
        year = data.get('year', '')

        if not roll or not roll.isdigit() or len(roll) != 6:
            return jsonify({"error": "Invalid roll number (6 digits required)"}), 400

        # FIX #6: Check roll + year + class (not just roll + year)
        with jobs_lock:
            for job_id, job in active_jobs.items():
                if (job.roll == roll and job.year == year and job.cls == cls
                        and job.status in ("pre_waiting", "waiting_for_result", "checking")):
                    return jsonify({
                        "job_id":  job_id,
                        "message": f"Already checking for Roll #{roll} ({cls})",
                        "status":  "already_running"
                    })

            job_id  = str(uuid.uuid4())[:8]
            checker = SmartResultChecker(roll, cls, year, job_id)
            active_jobs[job_id] = checker  # Protected by lock

        thread = threading.Thread(target=checker.start_smart_checking)
        thread.daemon = True
        thread.start()

        return jsonify({
            "job_id":  job_id,
            "message": f"Smart checker started for Roll #{roll} ({cls} {year}). "
                       f"Will wait for result upload, then check every 1-2 seconds!",
            "status":  "started"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/check-status/<job_id>', methods=['GET'])
def check_status(job_id):
    with jobs_lock:
        job = active_jobs.get(job_id)

    if not job:
        return jsonify({"error": "Job not found"}), 404

    elapsed  = (datetime.now() - job.start_time).total_seconds()
    minutes  = int(elapsed // 60)
    seconds  = int(elapsed % 60)
    rpm      = round((job.attempts / (elapsed / 60)), 1) if elapsed > 60 and job.status == "checking" else 0

    status_message = job.phase_message or {
        "waiting_for_result": f"⏳ Waiting for {job.year} result to be uploaded...",
        "checking":           f"⚡ Fast checking — {job.attempts} attempts so far",
        "completed":          "✅ Result found!",
        "stopped":            "⛔ Checker stopped",
        "error":              "❌ Error occurred",
    }.get(job.status, "")

    return jsonify({
        "status":               job.status,
        "attempts":             job.attempts,
        "result":               job.result,
        "roll":                 job.roll,
        "year":                 job.year,
        "cls":                  job.cls,
        "elapsed_time":         f"{minutes}m {seconds}s",
        "requests_per_minute":  rpm,
        "message":              status_message,
        "phase": (
            "waiting"      if job.status == "waiting_for_result" else
            "fast_checking" if job.status == "checking"          else
            "done"
        )
    })


@app.route('/stop-check/<job_id>', methods=['POST'])
def stop_check(job_id):
    with jobs_lock:
        job = active_jobs.get(job_id)

    if job:
        job.is_running = False
        job.status     = "stopped"
        return jsonify({"message": "Auto-check stopped"})

    return jsonify({"error": "Job not found"}), 404


@app.route('/check-year-availability', methods=['GET'])
def check_year_availability():
    year = request.args.get('year', '2026')
    try:
        url      = "https://bisesahiwal.edu.pk/allresult/"
        response = requests.get(url, timeout=10)
        if year in response.text:
            return jsonify({"available": True,  "year": year, "message": f"{year} result is available!"})
        return jsonify({"available": False, "year": year, "message": f"{year} result not uploaded yet"})
    except:
        return jsonify({"available": False, "year": year, "message": "Cannot check availability"})


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
