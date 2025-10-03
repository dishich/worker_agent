#!/data/data/com.termux/files/usr/bin/python
# -*- coding: utf-8 -*-

import os, sys, json, time, asyncio, hashlib, signal, re, socket
import aiohttp
from aiohttp import ClientSession
from yd_cloud import yadisk_download_cloud

from pathlib import Path
from subprocess import Popen, PIPE
from urllib.parse import urlparse

# ---- safe helper to await cancelled heartbeat task without crashing ws ----
async def _wait_cancel_safely(task):
    try:
        import asyncio
        await asyncio.wait_for(asyncio.shield(task), timeout=1)
    except asyncio.TimeoutError:
        pass
    except asyncio.CancelledError:
        pass


# отметка старта процесса агента
_AGENT_START_TS = time.time()

def make_heartbeat_snapshot():
    """Сбор метрик и формирование heartbeat в едином формате."""
    m = get_metrics()
    if m.get("uptime_s") is None:
        try:
            m["uptime_s"] = int(time.time() - _AGENT_START_TS)
        except Exception:
            m["uptime_s"] = 0
    return {
        "type": "heartbeat",
        "worker_id": WORKER_ID,
        "ts": int(time.time()),
        "status": os.environ.get("AGENT_STATUS", "idle"),
        "metrics": m,
        "software": {
            "termux": "0.118+",
            "ffmpeg": "installed",
            "python": sys.version.split()[0],
            "whisper_cli": "local_build"
        },
        "network": get_network_info(),
        "model_config": {
            "model_path": MODEL_PATH,
            "threads": THREADS, "lang_hint": LANG_HINT
        }
    }


# ================== ENV / константы ==================
WORKER_ID   = os.environ.get("WORKER_ID", "worker-UNKNOWN")
TOKEN       = os.environ.get("TOKEN", "REPLACE_WITH_REAL_TOKEN")

# ВАЖНО: SERVER_WS должен содержать ?token=... (сервер этого требует)
SERVER_WS   = os.environ.get("SERVER_WS") or f"wss://call-analysis-s6cb.onrender.com/ws/worker/{WORKER_ID}?token={TOKEN}"
SERVER_API  = os.environ.get("SERVER_API") or "https://call-analysis-s6cb.onrender.com/api/v1/job_result"

MODEL_PATH  = os.environ.get("MODEL_PATH", "/sdcard/worker/models/ggml-large-v3-q5_k.bin")
LANG_HINT   = os.environ.get("LANG_HINT", "ru")
THREADS     = int(os.environ.get("THREADS", "8"))
CURRENT_JOB = None


HEARTBEAT_INTERVAL_S = int(os.environ.get("HEARTBEAT_INTERVAL_S", "20"))
TIMEOUT_S            = int(os.environ.get("TIMEOUT_S", "7200"))

MAX_TEXT_LEN         = int(os.environ.get("MAX_TEXT_LEN", "200000"))  # лимит full_text
LOG_LEVEL  = os.environ.get("LOG_LEVEL", "INFO").upper()  # DEBUG/INFO/WARN/ERROR
REG_INCLUDE_TOKEN = os.environ.get("REG_INCLUDE_TOKEN") == "1"  # опц: включить токен в payload

# Таймауты HTTP (загрузка аудио/POST результата)
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=180, connect=10, sock_read=120)

# ================== папки ==================
BASE_DIR  = Path("/sdcard/worker")
CACHE_DIR = BASE_DIR / "cache"
LOG_DIR   = BASE_DIR / "logs"
for d in (CACHE_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

MAX_CACHE_MB = int(os.environ.get("MAX_CACHE_MB", "2048"))  # авто-очистка кэша

# ================== логирование ==================
def _should_debug():
    return LOG_LEVEL == "DEBUG"

# троттлинг повторяющихся сообщений при DEBUG=OFF
_last_msg = {}
def _throttle(key, sec=30):
    t = time.time()
    prev = _last_msg.get(key, 0)
    ok = (t - prev) >= sec
    if ok:
        _last_msg[key] = t
    return ok

def log(*a):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] " + " ".join(str(x) for x in a)
    print(line, flush=True)
    try:
        with (LOG_DIR / "agent.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def dbg(*a):
    if _should_debug():
        try:
            log(*a)
        except Exception:
            pass

# безопасный JSON-лог (объект → json либо str), усечённый
def slog(tag, obj):
    try:
        log(tag, json.dumps(obj, ensure_ascii=False)[:1000])
    except Exception:
        try:
            log(tag, str(obj)[:1000])
        except Exception:
            log(tag, "<unloggable-object>")

# ================== утилиты ==================
def run(cmd, timeout=None, env=None, log_cmd=False):
    """Запуск команды, возврат (rc, stdout, stderr)."""
    if log_cmd and _should_debug():
        log("CMD:", " ".join(cmd))
    p = Popen(cmd, stdout=PIPE, stderr=PIPE, text=True, env=env)
    try:
        out, err = p.communicate(timeout=timeout)
    except Exception:
        p.kill()
        out, err = p.communicate()
        return 124, out, err
    return p.returncode, out, err

def ensure_cache_quota():
    total = 0
    files = []
    for p in CACHE_DIR.glob("**/*"):
        if p.is_file():
            s = p.stat().st_size
            total += s
            files.append((p, s, p.stat().st_mtime))
    if total/1024/1024 <= MAX_CACHE_MB:
        return
    files.sort(key=lambda x: x[2])  # старые сначала
    while total/1024/1024 > MAX_CACHE_MB and files:
        p, s, _ = files.pop(0)
        try:
            p.unlink()
            total -= s
            log("CACHE: removed", p)
        except Exception as e:
            log("CACHE: rm error", p, e)

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def cleanup_files(*paths):
    for _p in paths:
        try:
            if _p and Path(_p).exists():
                Path(_p).unlink()
        except Exception:
            pass


def get_metrics():
    """
    Устойчивый расчёт метрик:
    - CPU% через /proc/stat с выборкой ~0.6s (настраивается env CPU_SAMPLE_S)
    - Если счётчики не изменились (tickless idle), вернём 0.0 вместо None
    - Остальные метрики как раньше, но с безопасными fallback
    """
    import time, os

    def read_cpu_fields():
        # Первая строка формата: "cpu  user nice system idle iowait irq softirq steal guest guest_nice ..."
        with open("/proc/stat", "r") as f:
            line = f.readline()
        parts = line.split()
        if not parts or parts[0] != "cpu":
            raise RuntimeError("bad /proc/stat")
        vals = [int(x) for x in parts[1:] if x.isdigit()]
        # классическая модель:
        # idle = idle + iowait (если есть),
        # nonidle = user + nice + system + irq + softirq + steal (если есть)
        idle = (vals[3] if len(vals) > 3 else 0) + (vals[4] if len(vals) > 4 else 0)
        nonidle = 0
        idx = 0
        fields = ["user","nice","system","idle","iowait","irq","softirq","steal","guest","guest_nice"]
        for i, v in enumerate(vals):
            name = fields[i] if i < len(fields) else None
            if name in ("user","nice","system","irq","softirq","steal"):
                nonidle += v
        total = idle + nonidle if (idle + nonidle) > 0 else sum(vals)
        return idle, total
    # --- CPU % ---
    cpu_percent = None
    try:
        idle1, total1 = read_cpu_fields()
        sample = float(os.environ.get("CPU_SAMPLE_S", "0.6"))
        if sample < 0.2:
            sample = 0.2
        time.sleep(sample)
        idle2, total2 = read_cpu_fields()
        didle  = idle2 - idle1
        dtotal = total2 - total1
        if dtotal > 0:
            cpu_percent = round(100.0 * (1.0 - (didle / dtotal)), 1)
            if cpu_percent < 0.0: cpu_percent = 0.0
            if cpu_percent > 100.0: cpu_percent = 100.0
        else:
            cpu_percent = 0.0
    except Exception:
        cpu_percent = None

    if cpu_percent is None:
        # нет доступа к /proc/stat → фолбэк по /proc/loadavg
        cpu_percent = _cpu_percent_from_loadavg()

    # --- Память ---
    mem_total_kb = None
    mem_free_kb = None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_free_kb = int(line.split()[1])
    except Exception:
        pass

    # --- Температура ---
    temp_c = None
    try:
        for cand in [
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/thermal/thermal_zone1/temp",
            "/sys/devices/virtual/thermal/thermal_zone0/temp",
        ]:
            if os.path.exists(cand):
                with open(cand) as f:
                    t = f.read().strip()
                if t.replace(".","",1).isdigit():
                    temp_c = float(t)/1000.0 if len(t) > 3 else float(t)
                    break
    except Exception:
        pass

    # --- Uptime ---
    uptime_s = None
    try:
        with open("/proc/uptime") as f:
            uptime_s = int(float(f.read().split()[0]))
    except Exception:
        # не критично
        pass

    # --- Disk free (/sdcard/worker) ---
    disk_free_mb = None
    try:
        st = os.statvfs(str(BASE_DIR))
        disk_free_mb = int(st.f_bavail * st.f_frsize / (1024*1024))
    except Exception:
        pass

    return {
        "cpu_percent": cpu_percent,
        "mem_total_kb": mem_total_kb,
        "mem_free_kb": mem_free_kb,
        "temp_c": temp_c,
        "uptime_s": uptime_s,
        "disk_free_mb": disk_free_mb,
    }


def adjust_threads_by_temp(temp_c):
    global THREADS
    if temp_c is None:
        return

def _cpu_percent_from_loadavg():
    """
    Фолбэк без root: оцениваем загрузку CPU через /proc/loadavg.
    Берём 1-минутный loadavg и делим на число ядер -> примерно в процентах.
    """
    try:
        with open("/proc/loadavg","r") as f:
            la1 = float(f.read().split()[0])
        import os
        cores = os.cpu_count() or 4
        val = round((la1 / cores) * 100.0, 1)
        if val < 0.0: val = 0.0
        if val > 100.0: val = 100.0
        return val
    except Exception:
        return 0.0

    if temp_c >= 75 and THREADS > 4:
        THREADS = 4; log("THERMAL:", temp_c, "→ THREADS=4")
    elif temp_c >= 68 and THREADS > 6:
        THREADS = 6; log("THERMAL:", temp_c, "→ THREADS=6")
    elif temp_c <= 60 and THREADS < 8:
        THREADS = 8; log("THERMAL:", temp_c, "→ THREADS=8")

def get_device_info():
    # модель — фикс, если вся партия одинакова
    model = "OPPO Find X2 Pro"
    try:
        cores = 0
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("processor"):
                    cores += 1
        cpu_cores = cores or os.cpu_count() or 8
    except Exception:
        cpu_cores = os.cpu_count() or 8
    # RAM
    ram_mb = None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    ram_mb = int(int(line.split()[1]) / 1024)
                    break
    except Exception:
        pass
    if not ram_mb:
        ram_mb = 12288
    # storage (по /sdcard/worker)
    storage_total_mb = None
    try:
        st = os.statvfs(str(BASE_DIR))
        storage_total_mb = int(st.f_blocks * st.f_frsize / (1024*1024))
    except Exception:
        pass
    if not storage_total_mb:
        storage_total_mb = 262144
    return {
        "model": model,
        "cpu_cores": int(cpu_cores),
        "ram_mb": int(ram_mb),
        "storage_total_mb": int(storage_total_mb)
    }

def get_network_info():
    # ip через ip route
    ip = None
    try:
        route = os.popen("ip -4 route get 1.1.1.1 2>/dev/null").read()
        m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", route)
        if m:
            ip = m.group(1)
    except Exception:
        pass
    # rtt до хоста WS
    host = urlparse(SERVER_WS).hostname or "call-analysis-s6cb.onrender.com"
    rtt_ms = None
    try:
        rc, out, err = run(["ping","-c","1","-W","1", host], timeout=2)
        m = re.search(r"time=([0-9.]+)\s*ms", out or "")
        if m:
            rtt_ms = float(m.group(1))
    except Exception:
        pass
    return {"ip": ip or "0.0.0.0", "rtt_ms": rtt_ms or 0}

def get_software_versions():
    py = sys.version.split()[0]
    ff_ver = "installed"
    try:
        rc, out, err = run(["ffmpeg","-version"], timeout=5)
        first = (out or "").splitlines()[0] if out else ""
        m = re.search(r"ffmpeg\s+version\s+([^\s]+)", first)
        if m:
            ff_ver = m.group(1)
    except Exception:
        pass
    return {"ffmpeg": ff_ver, "python": py}

_env_logged = False
def env_probe_once():
    """Один раз при старте: базовые проверки без спама."""
    global _env_logged
    if _env_logged:
        return
    try:
        rc, out, err = run(["getprop","ro.product.model"], timeout=3)
        model = (out or "").strip()
        if model:
            log("Device:", model)
    except Exception:
        pass
    sw = get_software_versions()
    log("FFmpeg:", sw.get("ffmpeg","unknown"), "Python:", sw.get("python","?"))
    net = get_network_info()
    dbg("Initial net:", net)
    _env_logged = True

# ================== сетевые операции ==================
async def http_download(session: ClientSession, url: str, dst: Path, timeout=120):
    log("DOWNLOAD:", url, "->", dst)
    async with session.get(url, timeout=timeout) as r:
        r.raise_for_status()
        with dst.open("wb") as f:
            async for chunk in r.content.iter_chunked(1 << 20):
                f.write(chunk)
    return dst





async def post_result(session: ClientSession, payload: dict):
    url = SERVER_API
    hdrs = {"Authorization": f"Bearer {TOKEN}"}
    hdrs["X-Worker-Id"] = WORKER_ID
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > 100_000:
        import gzip
        body = gzip.compress(body)
        hdrs["Content-Encoding"] = "gzip"
        hdrs["Content-Type"] = "application/json"
    else:
        hdrs["Content-Type"] = "application/json"
    log("POST result →", url, "(gzipped)" if hdrs.get("Content-Encoding")=="gzip" else "")
    async with session.post(url, headers=hdrs, data=body) as r:
        text = await r.text()
        log("POST status", r.status, text[:500])
        r.raise_for_status()

# ================== обработка заданий ==================

async def handle_job(session: ClientSession, ws, job: dict):
    """
    Ожидаем:
    {
      "type":"job.assign",
      "job_id":"...",
      "audio_url":"https://...mp3"  (опц.)
      "input": {
         "file": "/calls/...mp3"     (опц.)
        ,"channels": ["left","right"]
        ,"channel_roles": {"left":"operator","right":"client"}  (опц., присылает диспетчер)
      }
    }
    """
    slog("EVT:job.assign", job)
    job_id = job["job_id"]
    t0 = time.time()
    t_dl_ms = t_sp_ms = t_w_ms = 0
    j_input = job.get("input") or {}
    audio_url = job.get("audio_url") or None
    input_file = j_input.get("file") if isinstance(j_input, dict) else None

    # Роли каналов: если передали channel_roles — используем; иначе дефолт left/right
    channel_roles = j_input.get("channel_roles")
    if isinstance(channel_roles, dict) and channel_roles:
        channels = {k.lower(): (v or k).lower() for k,v in channel_roles.items()}
    else:
        channels = {"left": "left", "right": "right"}

    await ws.send_json({"type":"job.ack","job_id":job_id,"worker_id":WORKER_ID})
    slog("EVT:job.ack", {"job_id": job_id})

    # Файлы
    mp3_path    = CACHE_DIR / f"{job_id}.mp3"
    left_wav    = CACHE_DIR / f"{job_id}_left.wav"
    right_wav   = CACHE_DIR / f"{job_id}_right.wav"
    left_pref   = str(CACHE_DIR / f"{job_id}_left")
    right_pref  = str(CACHE_DIR / f"{job_id}_right")
    left_txt    = CACHE_DIR / f"{job_id}_left.txt"
    right_txt   = CACHE_DIR / f"{job_id}_right.txt"
    left_srt    = CACHE_DIR / f"{job_id}_left.srt"
    right_srt   = CACHE_DIR / f"{job_id}_right.srt"

    # Загрузка
    _t_dl0 = time.time()
    if audio_url:
        await http_download(session, audio_url, mp3_path, timeout=300)
    elif input_file:
        await yadisk_download_cloud(session, input_file, mp3_path, timeout=300)
    else:
        await ws.send_json({"type":"job.error","job_id":job_id,"worker_id":WORKER_ID,"error":{"code":"no_input","detail":"Neither audio_url nor input.file provided"}})
        slog("EVT:job.error", {"job_id": job_id, "error": "no_input"})
        return
    t_dl_ms = int((time.time() - _t_dl0) * 1000)

    # Разделение стерео
    _t_sp0 = time.time()
    rc, out, err = ffmpeg_split_stereo(mp3_path, left_wav, right_wav, timeout=TIMEOUT_S)
    t_sp_ms = int((time.time() - _t_sp0) * 1000)
    if rc != 0:
        log(f"ffmpeg split failed rc={rc}: {err[-400:]}")
        await ws.send_json({"type":"job.error","job_id":job_id,"worker_id":WORKER_ID,"error":"ffmpeg_split_failed"})
        slog("EVT:job.error", {"job_id": job_id, "error": "ffmpeg_split_failed"})
        cleanup_files(mp3_path, left_wav, right_wav)
        ensure_cache_quota()
        return

    # Проверка размеров WAV — если пустые, останавливаемся раньше
    try:
        if left_wav.stat().st_size < 1000 or right_wav.stat().st_size < 1000:
            await ws.send_json({"type":"job.error","job_id":job_id,"worker_id":WORKER_ID,"error":"split_empty_output"})
            slog("EVT:job.error", {"job_id": job_id, "error": "split_empty_output"})
            cleanup_files(mp3_path, left_wav, right_wav)
            ensure_cache_quota()
            return
    except Exception:
        pass

    # Параллельное распознавание в SRT
    _t_w0 = time.time()
    loop = asyncio.get_running_loop()
    tL = loop.run_in_executor(None, lambda: whisper_run_srt(left_wav,  left_pref, timeout=TIMEOUT_S))
    tR = loop.run_in_executor(None, lambda: whisper_run_srt(right_wav, right_pref, timeout=TIMEOUT_S))
    (rcL, outL, errL), (rcR, outR, errR) = await asyncio.gather(tL, tR)
    t_w_ms = int((time.time() - _t_w0) * 1000)

    srt_ok = left_srt.exists() and right_srt.exists()
    if (rcL != 0 or rcR != 0) and not srt_ok:
        log("whisper_srt rcL/rcR =", rcL, rcR)
        await ws.send_json({"type":"job.error","job_id":job_id,"worker_id":WORKER_ID,"error":"whisper_failed"})
        slog("EVT:job.error", {"job_id": job_id, "error": "whisper_failed", "rcL": rcL, "rcR": rcR, "stderrL_tail": (errL or "")[-400:], "stderrR_tail": (errR or "")[-400:]})
        cleanup_files(mp3_path, left_wav, right_wav)
        ensure_cache_quota()
        return
    # Парсинг сегментов
    segments = []
    if srt_ok:
        segL = _parse_srt_to_segments(left_srt,  "left")
        segR = _parse_srt_to_segments(right_srt, "right")

        # маппинг ролей: left/right -> operator/client (если так прислали)
        def _map_role(side: str) -> str:
            v = (channels.get(side) or side).lower()
            if v in ("operator","client"): return v
            if v in ("left","l"):  return "operator"
            if v in ("right","r"): return "client"
            return v

        for s in segL: s["speaker"] = _map_role("left")
        for s in segR: s["speaker"] = _map_role("right")
        segments = sorted(segL + segR, key=lambda x: (x.get("start") or 0, x.get("end") or 0))

        # если сегменты не распарсились, соберём plain из SRT
        if not segments:
            def _srt_plain_text(_p: Path) -> str:
                try:
                    t = _p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    t = ""
                lines = []
                for ln in (t.splitlines() if t else []):
                    s = ln.strip()
                    if not s: continue
                    if "-->" in s: continue
                    if s.isdigit(): continue
                    lines.append(s)
                return " ".join(lines)

            left_plain  = _srt_plain_text(left_srt)
            right_plain = _srt_plain_text(right_srt)

            def _role2(side: str) -> str:
                v = (channels.get(side) or side).lower()
                if v in ("operator","client"): return v
                if v in ("left","l"):  return "operator"
                if v in ("right","r"): return "client"
                return v

            if left_plain:
                segments.append({"speaker": _role2("left"),  "text": left_plain,  "start": None, "end": None})
            if right_plain:
                segments.append({"speaker": _role2("right"), "text": right_plain, "start": None, "end": None})

        full_text = " ".join(s.get("text","") for s in segments if s.get("text")).strip()

    else:
        # Fallback: TXT без таймкодов
        left_text  = left_txt.read_text(encoding="utf-8", errors="ignore") if left_txt.exists()  else ""
        right_text = right_txt.read_text(encoding="utf-8", errors="ignore") if right_txt.exists() else ""

        def _role(side: str) -> str:
            v = (channels.get(side) or side).lower()
            if v in ("operator","client"): return v
            if v in ("left","l"):  return "operator"
            if v in ("right","r"): return "client"
            return v

        segments = []
        if left_text.strip():
            segments.append({"speaker": _role("left"),  "text": left_text.strip(),  "start": None, "end": None})
        if right_text.strip():
            segments.append({"speaker": _role("right"), "text": right_text.strip(), "start": None, "end": None})

        full_text = " ".join(s.get("text","") for s in segments if s.get("text")).strip()

    # Очистка сегментов + ограничение текста
    segments = [s for s in segments if s.get("text")]
    for s in segments:
        s["text"] = _clean_segment_text(s["text"])
    segments = [s for s in segments if s["text"]]

    full_text = " ".join(s["text"] for s in segments).strip()
    if len(full_text) > MAX_TEXT_LEN:
        log(f"TEXT: truncated {len(full_text)} -> {MAX_TEXT_LEN}")
        full_text = full_text[:MAX_TEXT_LEN]
    # Слить соседние сегменты одного спикера и близкие по времени
    try:
        segments = _merge_adjacent_segments(segments, max_gap_s=float(os.environ.get("SEG_MERGE_GAP_S","0.6")))
        # Пересобрать full_text по сегментам (если он ещё не пустой)
        full_text = " ".join(s.get("text","") for s in segments if s.get("text"))
    except Exception:
        pass




    # Метрики
    metrics = {
        "download_ms": int(t_dl_ms),
        "split_ms": int(t_sp_ms),
        "whisper_ms": int(t_w_ms),
        "total_ms": int((time.time() - t0) * 1000),
    }

    # result_id для идемпотентности
    result_id = hashlib.sha256(
        (WORKER_ID + job_id + MODEL_PATH + str(len(full_text)) + str(len(segments))).encode("utf-8")
    ).hexdigest()

    # Итоговый payload
    payload = {
        "type": "job.result",
        "job_id": job_id,
        "worker_id": WORKER_ID,
        "status": "ok",
        "metrics": metrics,
        "text": full_text,
        "meta": {
            "segments": segments,
            "audio_sha256": sha256_file(mp3_path),
            "model_path": MODEL_PATH,
            "lang_hint": LANG_HINT,
            "threads": THREADS,
            "result_id": result_id,
        },
    }
    # Пути до артефактов для отладки
    try:
        if left_srt.exists():  payload["meta"]["left_srt_path"]  = str(left_srt)
        if right_srt.exists(): payload["meta"]["right_srt_path"] = str(right_srt)
    except Exception:
        pass

    await post_result(session, payload)
    await ws.send_json({"type":"job.done","job_id":job_id,"worker_id":WORKER_ID})
    slog("EVT:job.done", {"job_id": job_id, "segments_cnt": len(segments)})

    cleanup_files(mp3_path, left_wav, right_wav)
    ensure_cache_quota()


async def recv_json(ws, *, first=False, timeout=None):
    """Унифицированный приём JSON-кадра с понятными ошибками + лог входящих кадров."""
    msg = await ws.receive(timeout=timeout) if timeout else await ws.receive()
    if msg.type != aiohttp.WSMsgType.TEXT:
        raise RuntimeError(f"{'registration_' if first else ''}bad_frame: {msg.type}")
    try:
        data = json.loads(msg.data)
    except Exception:
        head = msg.data[:200] if isinstance(msg.data, str) else str(msg.data)
        raise RuntimeError(f"{'registration_' if first else ''}bad_json: {head}")
    try:
        if first:
            slog("EVT:ws.recv.first", data)
        elif _should_debug():
            slog("EVT:ws.recv", data)
    except Exception:
        pass
    return data

async def heartbeat_loop(ws):
    while True:
        m = get_metrics()
        # fallback: если системный uptime не доступен — используем аптайм процесса агента
        if m.get("uptime_s") is None:
            try:
                import time as _t2
                m["uptime_s"] = int(_t2.time() - _AGENT_START_TS)
            except Exception:
                m["uptime_s"] = 0
        adjust_threads_by_temp(m.get("temp_c"))
        hb = {
            "type":"heartbeat",
            "worker_id": WORKER_ID,
            "ts": int(time.time()),
            "status": os.environ.get("AGENT_STATUS","idle"),
            "metrics": m,
            "software": {
                "termux":"0.118+",
                "ffmpeg":"installed",
                "python": sys.version.split()[0],
                "whisper_cli":"local_build"
            },
            "network": get_network_info(),
            "model_config": {
                "model_path": MODEL_PATH,
                "threads": THREADS,
                "lang_hint": LANG_HINT
            }
        }
        # Не пытаться слать HB в закрытый сокет
        if getattr(ws, 'closed', False):
            log('HB: ws is closed → stop loop')
            break
        try:
            await ws.send_json(hb)
            slog("EVT:heartbeat.sent", hb)
        except Exception as e:
            log("HB send error:", e)
            break
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
        except asyncio.CancelledError:
            break

# ================== основной цикл ==================
async def main():
    global THREADS, LANG_HINT
    log("START agent", WORKER_ID)
    env_probe_once()  # выполняется один раз при старте процесса

    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    backoff = 1

    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
        while True:
            try:
                log("WS connect →", SERVER_WS)
                async with session.ws_connect(
                    SERVER_WS,
                    headers=headers,
                    heartbeat=HEARTBEAT_INTERVAL_S,
                    max_msg_size=64 * 1024 * 1024
                ) as ws:
                    log("WS connected ✓")

                    # --- registration: ОДИН РАЗ на соединение ---
                    reg = {
                        "type": "registration",
                        "worker_id": WORKER_ID,
                        "device": get_device_info(),
                        "software": get_software_versions(),
                        "capabilities": {"supports_models": [os.path.basename(MODEL_PATH)]},
                        "model_config": {"model_path": MODEL_PATH, "threads": THREADS, "lang_hint": LANG_HINT},
                        "network": get_network_info()
                    }
                    if REG_INCLUDE_TOKEN:
                        reg["token"] = TOKEN  # включать только если сервер требует это дополнительно

                    # аккуратный лог без утечки токена
                    slog("EVT:registration.sent", {k: ("****" if k=="token" else v) for k,v in reg.items() if k != "device"})
                    await ws.send_json(reg)

                    # ждём подтверждение регистрации (server должен вернуть registration.ok)
                    while True:
                        data = await recv_json(ws, first=True, timeout=30)
                        t = data.get("type")
                        if t == "registration.ok" and data.get("worker_id") == WORKER_ID:
                            log("registration.ok")
                            # немедленный однократный heartbeat для верификации канала
                            try:
                                hb_once = {
                                    "type":"heartbeat","worker_id":WORKER_ID,"ts":int(time.time()),
                                    "status": os.environ.get("AGENT_STATUS","idle"),
                                    "metrics": get_metrics(),
                                    "software": {"python": sys.version.split()[0]},
                                    "network": get_network_info(),
                                    "model_config": {"model_path": MODEL_PATH,"threads": THREADS,"lang_hint": LANG_HINT}
                                }
                                await ws.send_json(hb_once)
                                slog("EVT:heartbeat.sent.immediate", hb_once)
                            except Exception as _e:
                                log("EVT:heartbeat.immediate.error", repr(_e))
                            break
                        if t == "control.ping":
                            await ws.send_json({"type": "control.pong", "worker_id": WORKER_ID})
                            log("control.ping → pong (до registration.ok)")
                            continue
                        # если пришло что-то иное на этапе рукопожатия — ошибка
                        raise RuntimeError(f"registration_failed: {data}")

                    # heartbeat
                    hb_task = asyncio.create_task(heartbeat_loop(ws))

                    # основной цикл сообщений
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                            except Exception:
                                dbg("WS non-json:", msg.data[:300])
                                continue
                            slog("EVT:ws.recv.loop", data)
                            t = data.get("type")

                            if t == "job.assign":

                                global CURRENT_JOB

                                if CURRENT_JOB and not CURRENT_JOB.done():

                                    await ws.send_json({"type":"job.error","job_id":data.get("job_id"),"worker_id":WORKER_ID,"error":{"code":"busy","detail":"Worker is processing another job"}})

                                    continue


                                os.environ["AGENT_STATUS"] = "busy"


                                async def _run_job():

                                    try:

                                        await handle_job(session, ws, data)

                                    except Exception as e:

                                        log("job task error:", repr(e))

                                        try:

                                            await ws.send_json({"type":"job.error","job_id":data.get("job_id"),"worker_id":WORKER_ID,"error":{"code":"exception","detail":repr(e)}})

                                        except Exception:

                                            pass

                                    finally:

                                        os.environ["AGENT_STATUS"] = "idle"


                                CURRENT_JOB = asyncio.create_task(_run_job())

                                continue

                            elif t == "control.set_config":
                                THREADS = int(data.get("threads", THREADS))
                                LANG_HINT = data.get("lang_hint", LANG_HINT)
                                await ws.send_json({"type":"control.ack","worker_id":WORKER_ID})
                            elif t == "control.ping":
                                slog("EVT:control.ping", data)
                                await ws.send_json({"type":"control.pong","worker_id":WORKER_ID})
                                log("EVT:control.pong.sent", WORKER_ID)
                            elif t == "error":
                                log("server error:", data)
                            else:
                                dbg("WS msg:", data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            raise RuntimeError(f"WS closed: {msg.type}")

                    # нормальный выход из цикла означает закрытие сокета
                    if not hb_task.done():
                        hb_task.cancel()
                    backoff = 1  # сбросить бэкофф после успешной сессии

            except Exception as e:
                log("WS error:", repr(e), "reconnect in", backoff, "s")
                await asyncio.sleep(backoff)
                backoff = min(backoff*2, 60)

# --- FFmpeg: разложить стерео в два моно WAV 16 kHz ---
def ffmpeg_split_stereo(src_mp3: Path, left_wav: Path, right_wav: Path, timeout=TIMEOUT_S):
    """Split stereo MP3 into 2 mono WAV 16kHz in ONE ffmpeg run (faster, fewer I/O ops)."""
    left_wav.parent.mkdir(parents=True, exist_ok=True)
    right_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg","-y","-hide_banner","-loglevel","error",
        "-i", str(src_mp3),
        "-filter_complex","[0:a]channelsplit=channel_layout=stereo[FL][FR]",
        "-map","[FL]","-ar","16000","-ac","1", str(left_wav),
        "-map","[FR]","-ar","16000","-ac","1", str(right_wav),
    ]
    rc, out, err = run(cmd, timeout=timeout)
    if rc != 0:
        log("ffmpeg_split(one-pass) failed:", (err or "")[-400:])
        return rc, out, err
    return 0, out, err

# --- Whisper.cpp launcher (создаёт <out_prefix>.txt) ---

def whisper_run(wav_path: Path, out_prefix: str, timeout=3600):
    """
    Запускает whisper.cpp и сохраняет результат в <out_prefix>.txt.
    Без использования несуществующих флагов прогресса. Поддерживает разные варианты вывода
    у бинарей `main` и `whisper-cli`.
    Возвращает (rc, stdout, stderr). rc=0 при наличии .txt, иначе rc=2.
    """
    import os, shutil

    # --- выбираем бинарь ---
    candidates = []
    env_bin = os.environ.get("WHISPER_BIN")
    if env_bin and os.path.exists(env_bin) and os.access(env_bin, os.X_OK):
        candidates.append(env_bin)
    home = Path.home()
    candidates.append(str(home / "worker_agent" / "whisper.cpp" / "build" / "bin" / "whisper-cli"))
    pth = shutil.which("whisper-cli")
    if pth:
        candidates.append(pth)
    candidates.append(str(home / "worker_agent" / "whisper.cpp" / "build" / "bin" / "main"))

    exe = next((c for c in candidates if os.path.exists(c) and os.access(c, os.X_OK)), None)
    if not exe:
        return 127, "", "whisper binary not found (set WHISPER_BIN or build whisper-cli)"

    Path(out_prefix).parent.mkdir(parents=True, exist_ok=True)
    out_txt = Path(f"{out_prefix}.txt")

    # Базовые аргументы (без флагов прогресса)
    base = [exe, "-m", str(MODEL_PATH), "-f", str(wav_path), "-l", str(LANG_HINT), "-t", str(THREADS)]

    # Совместимые стратегии вывода для разных версий CLI
    strategies = [
        ["-of", str(out_prefix), "-otxt"],          # main / часть сборок
        ["-of", str(out_prefix), "--output-txt"],   # редкий алиас
        ["--output-file", str(out_txt)],            # некоторые whisper-cli
        ["-o", str(out_txt)],                       # короткий вариант
    ]

    tried = []
    last_rc, last_out, last_err = 1, "", ""

    def _run(cmd):
        return run(cmd, timeout=timeout, log_cmd=True)

    # Пробуем последовательно
    for strat in strategies:
        if out_txt.exists():
            try: out_txt.unlink()
            except Exception: pass
        cmd = base + strat
        rc, out, err = _run(cmd)
        tried.append(" ".join(cmd))
        if out_txt.exists():
            return 0, out, err
        last_rc, last_out, last_err = rc, out, err

    # Ничего не сработало — вернём понятную ошибку
    tail = (last_err or "")[-1200:]
    msg = (last_err or "") + f" OUTPUT_MISSING:{out_txt} TRIED_VARIANTS:{len(tried)}"
    if last_rc == 0:
        last_rc = 2
    return last_rc, last_out, msg


# --- Whisper JSON helper (создаёт <out_prefix>.json) ---
def whisper_run_json(wav_path: Path, out_prefix: str, timeout=3600):
    """
    Запускает whisper.cpp и сохраняет JSON-сегменты в <out_prefix>.json (ключ 'segments').
    Возвращает (rc, stdout, stderr). rc=0 при наличии .json, иначе rc=2.
    """
    import os, shutil
    # выбор бинаря — как в whisper_run
    candidates = []
    env_bin = os.environ.get("WHISPER_BIN")
    if env_bin and os.path.exists(env_bin) and os.access(env_bin, os.X_OK):
        candidates.append(env_bin)
    home = Path.home()
    candidates.append(str(home / "worker_agent" / "whisper.cpp" / "build" / "bin" / "whisper-cli"))
    pth = shutil.which("whisper-cli")
    if pth:
        candidates.append(pth)
    candidates.append(str(home / "worker_agent" / "whisper.cpp" / "build" / "bin" / "main"))
    exe = next((c for c in candidates if os.path.exists(c) and os.access(c, os.X_OK)), None)
    if not exe:
        return 127, "", "whisper binary not found (set WHISPER_BIN or build whisper-cli)"

    out_json = Path(f"{out_prefix}.json")
    if out_json.exists():
        try: out_json.unlink()
        except Exception: pass

    base = [exe, "-m", str(MODEL_PATH), "-f", str(wav_path), "-l", str(LANG_HINT), "-t", str(THREADS)]
    variants = [
        ["-of", str(out_prefix), "-oj"],           # main
        ["--output-json", str(out_json)],          # whisper-cli
    ]
    last_rc, last_out, last_err = 1, "", ""
    for v in variants:
        if out_json.exists():
            try: out_json.unlink()
            except Exception: pass
        rc, out, err = run(base + v, timeout=timeout, log_cmd=True)
        if out_json.exists():
            return 0, out, err
        last_rc, last_out, last_err = rc, out, err
    if last_rc == 0:
        last_rc = 2
    return last_rc, last_out, (last_err or "") + f" OUTPUT_JSON_MISSING:{out_json}"


# --- Whisper SRT helper (создаёт <out_prefix>.srt) ---
def whisper_run_srt(wav_path: Path, out_prefix: str, timeout=3600):
    """
    Run whisper.cpp and save SRT in <out_prefix>.srt.
    Returns (rc, stdout, stderr). rc=0 if .srt exists, else rc=2.
    """
    import os, shutil
    candidates = []
    env_bin = os.environ.get("WHISPER_BIN")
    if env_bin and os.path.exists(env_bin) and os.access(env_bin, os.X_OK):
        candidates.append(env_bin)
    home = Path.home()
    candidates.append(str(home / "worker_agent" / "whisper.cpp" / "build" / "bin" / "whisper-cli"))
    pth = shutil.which("whisper-cli")
    if pth:
        candidates.append(pth)
    candidates.append(str(home / "worker_agent" / "whisper.cpp" / "build" / "bin" / "main"))
    exe = next((c for c in candidates if os.path.exists(c) and os.access(c, os.X_OK)), None)
    if not exe:
        return 127, "", "whisper binary not found (set WHISPER_BIN or build whisper-cli)"
    out_srt = Path(f"{out_prefix}.srt")
    if out_srt.exists():
        try: out_srt.unlink()
        except Exception: pass
    base = [exe, "-m", str(MODEL_PATH), "-f", str(wav_path), "-l", str(LANG_HINT), "-t", str(THREADS)]
    variants = [
        ["-of", str(out_prefix), "-osrt"],     # main
        ["--output-srt", str(out_srt)],        # whisper-cli
    ]
    last_rc, last_out, last_err = 1, "", ""
    for v in variants:
        if out_srt.exists():
            try: out_srt.unlink()
            except Exception: pass
        rc, out, err = run(base + v, timeout=timeout, log_cmd=True)
        if out_srt.exists():
            return 0, out, err
        last_rc, last_out, last_err = rc, out, err
    if last_rc == 0:
        last_rc = 2
    return last_rc, last_out, (last_err or "") + f" OUTPUT_SRT_MISSING:{out_srt}"

def _parse_srt_to_segments(path: Path, speaker: str):
    """
    Parse .srt file into list of segments: [{'speaker','text','start','end'}, ...]
    """
    import re
    if not path.exists():
        return []
    data = path.read_text(encoding="utf-8", errors="ignore")
    blocks = re.split(r"\r?\n\r?\n+", data.strip())
    segs = []
    def _t2s(t):
        h,m,s_ms = t.split(":")
        s,ms = s_ms.split(",")
        return int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000.0
    for b in blocks:
        lines = [ln for ln in b.splitlines() if ln.strip()!=""]
        if len(lines) < 2: continue
        if "-->" in lines[0]:
            ts_line = lines[0]; text_lines = lines[1:]
        elif len(lines)>1 and "-->" in lines[1]:
            ts_line = lines[1]; text_lines = lines[2:]
        else: continue
        m = re.search(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})", ts_line)
        if not m: continue
        st = _t2s(m.group(1)); en = _t2s(m.group(2))
        tx = " ".join(t.strip() for t in text_lines).strip()
        if tx: segs.append({"speaker": speaker, "text": tx, "start": st, "end": en})
    return segs

def _clean_segment_text(t: str) -> str:
    # простая чистка сегментов от мусора
    import re as _re
    if not t:
        return ""
    t = _re.sub(r'\s+', ' ', t).strip()
    low = t.lower()
    if low in {"аплодисменты","[аплодисменты]","(аплодисменты)","(шум)","[шум]","(музыка)","[музыка]","applause"}:
        return ""
    if len(t) < 3:
        return ""
    return t


def _merge_adjacent_segments(segs, max_gap_s=0.6):
    """
    Сливает соседние сегменты одного и того же speaker'а, если пауза между ними <= max_gap_s.
    Возвращает новый список сегментов, отсортированный по времени.
    """
    if not isinstance(segs, list):
        return segs
    norm = []
    for s in segs:
        try:
            norm.append({
                "speaker": s.get("speaker"),
                "text": (s.get("text") or "").strip(),
                "start": s.get("start"),
                "end": s.get("end"),
            })
        except Exception:
            pass
    norm = sorted(norm, key=lambda x: ((x.get("start") or 0), (x.get("end") or 0)))
    out = []
    for s in norm:
        if not out:
            out.append(s); continue
        prev = out[-1]
        can_merge = (
            prev.get("speaker") == s.get("speaker") and
            prev.get("end") is not None and
            s.get("start") is not None and
            (s["start"] - prev["end"] <= max_gap_s)
        )
        if can_merge:
            prev["end"] = s.get("end", prev["end"])
            prev["text"] = (prev.get("text","") + " " + s.get("text","")).strip()
        else:
            out.append(s)
    return out

# ================== entrypoint ==================
if __name__ == "__main__":
    # гарантируем немедленный вывод
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    # маленький баннер старта (чтобы не было «тихого» выхода)
    try:
        log("START agent", WORKER_ID)
        sw = get_software_versions()
        log("FFmpeg:", sw.get("ffmpeg","?"), "Python:", sw.get("python","?"))
    except Exception:
        print("START agent", WORKER_ID, flush=True)

    # опционально ускоряем event loop, если uvloop установлен
    try:
        import uvloop  # type: ignore
        uvloop.install()
        dbg("uvloop installed")
    except Exception:
        pass

    # аккуратное завершение по Ctrl+C/TERM
    stop = asyncio.Event()

    def _graceful(signum, _frame):
        try:
            log(f"signal {signum} \u2192 shutdown")
        except Exception:
            pass
        stop.set()

    for _sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if _sig:
            try:
                signal.signal(_sig, _graceful)
            except Exception:
                pass

    async def _runner():
        # запускаем основную корутину и ждём сигнала на остановку
        task = asyncio.create_task(main())
        done, pending = await asyncio.wait(
            {task, asyncio.create_task(stop.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        # если пришёл сигнал — отменим main()
        if stop.is_set() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(_runner())
    except KeyboardInterrupt:
        log("EXIT: KeyboardInterrupt")
    except Exception as e:
        # чтобы не «тихо» умирал
        try:
            log("FATAL:", repr(e))
        finally:
            raise

