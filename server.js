const express = require('express');
const axios = require('axios');
const cheerio = require('cheerio');
const { v4: uuidv4 } = require('uuid');
const path = require('path');

const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

const activeJobs = {};

// Cleanup old jobs every 5 minutes
setInterval(() => {
    const now = Date.now();
    for (const [jid, job] of Object.entries(activeJobs)) {
        if (['completed', 'stopped', 'error'].includes(job.status)) {
            if (now - job.startTime > 600000) {
                delete activeJobs[jid];
                console.log(`[Cleanup] Removed job ${jid}`);
            }
        }
    }
}, 300000);


function safeInt(val) {
    if (!val) return 0;
    const cleaned = String(val).trim().replace(/[-–\s]/g, '');
    const n = parseInt(cleaned);
    return isNaN(n) ? 0 : n;
}

function getFormAction(html, baseUrl) {
    const $ = cheerio.load(html);
    const action = $('form').attr('action');
    if (action) {
        if (action.startsWith('http')) return action;
        return baseUrl.replace(/\/$/, '') + '/' + action.replace(/^\//, '');
    }
    return baseUrl + 'route.php';
}

async function checkSingleTime(job) {
    try {
        const url = 'https://bisesahiwal.edu.pk/allresult/';
        const headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': url
        };

        const res = await axios.get(url, { headers, timeout: 8000 });
        const $ = cheerio.load(res.data);

        // Get CSRF token
        let token = '';
        const tokenInput = $('input[name="csrf_token"]').val()
            || $('input[name="_token"]').val()
            || $('input[name="token"]').val()
            || $('input[type="hidden"]').first().val()
            || '';
        token = tokenInput;

        const formAction = getFormAction(res.data, url);

        const params = new URLSearchParams();
        params.append('class', job.cls === '9th' ? '1' : '2');
        params.append('year', job.year);
        params.append('sess', '1');
        params.append('rno', job.roll);
        params.append('csrf_token', token);
        params.append('commit', 'GET RESULT');

        const res2 = await axios.post(formAction, params.toString(), {
            headers: {
                ...headers,
                'Content-Type': 'application/x-www-form-urlencoded'
            },
            timeout: 8000
        });

        const $2 = cheerio.load(res2.data);
        const results = [];
        let totalMarks = null;

        $2('tr').each((_, row) => {
            const cols = $2(row).find('td').map((_, td) => $2(td).text().trim()).get();
            if (cols.length < 4) return;

            const subject = cols[1] ? cols[1].toUpperCase() : '';
            if (!subject) return;

            if (job.cls === '9th') {
                const marks = safeInt(cols[3]);
                if (marks > 0) results.push({ subject, marks });
            } else {
                const marks9    = safeInt(cols[3]);
                const marks10   = safeInt(cols[4]);
                const practical = safeInt(cols[5]);
                const total     = marks9 + marks10 + practical;
                if (total > 0) results.push({ subject, total, class9: marks9, class10: marks10, practical });
            }

            if (cols[0] && cols[0].toUpperCase().includes('TOTAL')) {
                totalMarks = cols[cols.length - 1];
            }
        });

        if (results.length > 0) {
            return { success: true, results, total: totalMarks, attempts: job.attempts };
        }
        return null;

    } catch (e) {
        console.log(`[${job.jobId}] Attempt failed: ${e.message}`);
        return null;
    }
}

async function isYearAvailable(job) {
    try {
        const url = 'https://bisesahiwal.edu.pk/allresult/';
        const headers = { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' };
        const res = await axios.get(url, { headers, timeout: 10000 });
        const $ = cheerio.load(res.data);

        // Check 1: dropdown
        let foundInDropdown = false;
        $('select[name="year"] option').each((_, el) => {
            if ($(el).text().includes(job.year)) foundInDropdown = true;
        });
        if (foundInDropdown) {
            console.log(`[${job.jobId}] Year ${job.year} found in dropdown!`);
            return true;
        }

        // Check 2: page text
        if (res.data.includes(job.year)) {
            console.log(`[${job.jobId}] Year ${job.year} found in page text!`);
            return true;
        }

        // Check 3: direct result fetch
        const test = await checkSingleTime(job);
        if (test) {
            console.log(`[${job.jobId}] Year ${job.year} confirmed via result fetch!`);
            return true;
        }

        console.log(`[${job.jobId}] Year ${job.year} not available yet.`);
        return false;

    } catch (e) {
        console.log(`[${job.jobId}] Error checking availability: ${e.message}`);
        return false;
    }
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function workerThread(job, workerId, foundRef) {
    console.log(`[${job.jobId}] Worker-${workerId} started`);

    while (job.isRunning && !foundRef.found) {
        job.attempts++;
        const result = await checkSingleTime(job);

        if (result) {
            if (!foundRef.found) {
                foundRef.found = true;
                job.result = result;
                job.status = 'completed';
                job.phaseMessage = `Result found after ${job.attempts} attempts!`;
                console.log(`[${job.jobId}] Worker-${workerId} FOUND RESULT!`);
            }
            break;
        }

        // 0.3 - 0.5s delay per worker → 3 workers = ~360-600 req/min
        await sleep(300 + Math.random() * 200);
    }
}

async function startSmartChecking(job) {
    // Phase 1: Wait for year
    job.status = 'waiting_for_result';
    job.phaseMessage = `Waiting for ${job.year} result on BISE website...`;
    console.log(`[${job.jobId}] Phase 1 started`);

    while (job.isRunning && job.status === 'waiting_for_result') {
        const available = await isYearAvailable(job);
        if (available) {
            job.status = 'checking';
            job.phaseMessage = 'Result uploaded! 3 parallel workers launched!';
            console.log(`[${job.jobId}] Phase 2 started!`);
            break;
        }
        await sleep(30000);
    }

    if (!job.isRunning) {
        job.status = 'stopped';
        return;
    }

    // Phase 2: 3 parallel workers
    const foundRef = { found: false };
    const workers = [];

    for (let i = 1; i <= 3; i++) {
        workers.push(workerThread(job, i, foundRef));
        await sleep(100);
    }

    await Promise.race([
        Promise.all(workers),
        new Promise(resolve => {
            const check = setInterval(() => {
                if (!job.isRunning || foundRef.found) {
                    clearInterval(check);
                    resolve();
                }
            }, 200);
        })
    ]);

    if (!foundRef.found) job.status = 'stopped';
}


// ── Routes ────────────────────────────────────────────────────────────────────

app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.post('/start-auto-check', (req, res) => {
    try {
        const { roll, class: cls, year } = req.body;

        if (!roll || !/^\d{6}$/.test(roll)) {
            return res.status(400).json({ error: 'Invalid roll number (6 digits required)' });
        }

        // Check duplicate
        for (const [jid, job] of Object.entries(activeJobs)) {
            if (job.roll === roll && job.year === year && job.cls === cls
                && ['waiting_for_result', 'checking'].includes(job.status)) {
                return res.json({ job_id: jid, message: `Already checking Roll #${roll}`, status: 'already_running' });
            }
        }

        const jobId = uuidv4().slice(0, 8);
        const job = {
            jobId, roll, cls, year,
            status: 'waiting_for_result',
            attempts: 0,
            result: null,
            error: null,
            startTime: Date.now(),
            isRunning: true,
            phaseMessage: ''
        };

        activeJobs[jobId] = job;
        startSmartChecking(job); // async, don't await

        return res.json({
            job_id: jobId,
            message: `Smart checker started for Roll #${roll} (${cls} ${year})`,
            status: 'started'
        });

    } catch (e) {
        return res.status(500).json({ error: e.message });
    }
});

app.get('/check-status/:jobId', (req, res) => {
    const job = activeJobs[req.params.jobId];
    if (!job) return res.status(404).json({ error: 'Job not found' });

    const elapsed = (Date.now() - job.startTime) / 1000;
    const minutes = Math.floor(elapsed / 60);
    const seconds = Math.floor(elapsed % 60);
    const rpm = elapsed > 60 && job.status === 'checking'
        ? Math.round((job.attempts / (elapsed / 60)) * 10) / 10 : 0;

    const defaultMsg = {
        waiting_for_result: `Waiting for ${job.year} result...`,
        checking: `Fast checking — ${job.attempts} attempts`,
        completed: 'Result found!',
        stopped: 'Checker stopped',
        error: 'Error occurred'
    };

    return res.json({
        status: job.status,
        attempts: job.attempts,
        result: job.result,
        roll: job.roll,
        year: job.year,
        cls: job.cls,
        elapsed_time: `${minutes}m ${seconds}s`,
        requests_per_minute: rpm,
        message: job.phaseMessage || defaultMsg[job.status] || '',
        phase: job.status === 'waiting_for_result' ? 'waiting'
             : job.status === 'checking' ? 'fast_checking' : 'done'
    });
});

app.post('/stop-check/:jobId', (req, res) => {
    const job = activeJobs[req.params.jobId];
    if (job) {
        job.isRunning = false;
        job.status = 'stopped';
        return res.json({ message: 'Auto-check stopped' });
    }
    return res.status(404).json({ error: 'Job not found' });
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Server running on port ${PORT}`));
