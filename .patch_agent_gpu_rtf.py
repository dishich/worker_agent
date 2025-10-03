import re, sys, time, json, pathlib
p = pathlib.Path("agent.py")
s = p.read_text(encoding="utf-8")

def ins(after_pat, block, once_name):
    if once_name in s:
        return None
    m = re.search(after_pat, s, re.DOTALL)
    if not m: 
        print(f"[WARN] anchor not found for {once_name}", file=sys.stderr)
        return None
    pos = m.end()
    return s[:pos] + "\n\n" + block.strip() + "\n\n" + s[pos:]

# 0) глобалы: CURRENT_JOB уже есть. Добавим USE_GPU и MODEL_PATH_FALLBACK
if "MODEL_PATH_FALLBACK" not in s:
    s = s.replace("MODEL_PATH  = os.environ.get(\"MODEL_PATH\", \"/sdcard/worker/models/ggml-large-v3-q5_k.bin\")",
                  "MODEL_PATH  = os.environ.get(\"MODEL_PATH\", \"/sdcard/worker/models/ggml-large-v3-q5_k.bin\")\nMODEL_PATH_FALLBACK = os.environ.get(\"MODEL_PATH_FALLBACK\")")
if "USE_GPU" not in s:
    s = s.replace("CURRENT_JOB = None", "CURRENT_JOB = None\nUSE_GPU = None  # autodetected at runtime")

# 1) utils: ffprobe_duration()
if "def ffprobe_duration(" not in s:
    s = ins(r"# ================== утилиты ==================", r"""
def ffprobe_duration(path: Path) -> float:
    """ + '"""' + r"""Вернёт длительность файла (сек) через ffprobe, либо 0.0 при ошибке.""" + '"""' + r"""
    try:
        rc,out,err = run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=nw=1:nk=1", str(path)], timeout=15)
        if rc==0:
            return float(out.strip())
    except Exception:
        pass
    return 0.0
""", "ffprobe_duration")

# 2) utils: detect_vulkan_gpu() (разово дергаем vulkaninfo и проверяем, что устройство не llvmpipe CPU)
if "def detect_vulkan_gpu(" not in s:
    s = ins(r"def get_software_versions\(\):.*?return \{.*?\}\n", r"""
def detect_vulkan_gpu() -> bool:
    """
    Проверяет наличие реального Vulkan GPU (не llvmpipe CPU).
    Возвращает True, если найдено устройство с deviceType != CPU.
    """
    try:
        rc,out,err = run(["vulkaninfo"], timeout=10)
        if rc!=0 or not out:
            return False
        # Быстрые эвристики
        if "VULKANINFO" not in out:
            return False
        # В секции Device Properties ищем deviceType
        # Если встречается PHYSICAL_DEVICE_TYPE_CPU и нет DISCRETE/INTEGRATED — значит только CPU.
        has_gpu = any(x in out for x in ("PHYSICAL_DEVICE_TYPE_DISCRETE_GPU","PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU"))
        has_only_cpu = ("PHYSICAL_DEVICE_TYPE_CPU" in out) and not has_gpu
        return bool(has_gpu and not has_only_cpu)
    except Exception:
        return False
""", "detect_vulkan_gpu")

# 3) whisper_run: добавить стратегии с --gpu 1, включаем их если USE_GPU True
if "TRIED_VARIANTS" in s and "--gpu" not in s:
    s = re.sub(r"(\s*strategies\s*=\s*\[\s*\n)([^\]]+\])",
               r"""\1        # GPU-first (если бэкенд поддерживается)\n        ["-of", str(out_prefix), "-otxt", "--gpu", "1"],\n        ["-of", str(out_prefix), "--output-txt", "--gpu", "1"],\n        ["--output-file", str(out_txt), "--gpu", "1"],\n        ["-o", str(out_txt), "--gpu", "1"],\n    ]\n    # затем CPU-варианты (fallback)\n    strategies_cpu = [\n\2""",
               s, flags=re.DOTALL)
    s = s.replace("for strat in strategies:", "for strat in (strategies if (USE_GPU) else []) + strategies_cpu:")

# 4) в main(): определить USE_GPU один раз после регистрации
if "USE_GPU =" in s and "detect_vulkan_gpu()" in s and "autodetect Vulkan" not in s:
    s = s.replace("log(\"WS connected ✓\")",
                  "log(\"WS connected ✓\")\n\n                    # autodetect Vulkan once per session\n                    global USE_GPU\n                    if USE_GPU is None:\n                        USE_GPU = detect_vulkan_gpu()\n                        log(\"GPU(Vulkan) available:\", USE_GPU)")

# 5) handle_job: посчитать длительности и RTF + fallback на запасную модель при ошибке
if "rtf_left" not in s:
    s = s.replace(
        "# split\n    _t_sp0 = time.time()",
        "# split\n    _t_sp0 = time.time()\n    # длительность для метрик\n    mp3_dur = ffprobe_duration(mp3_path)\n    # WAV длительности посчитаем после сплита"
    )

    s = s.replace(
        "t_w_ms = int((time.time() - _t_w0) * 1000)",
        "t_w_ms = int((time.time() - _t_w0) * 1000)\n\n    # посчитаем длительность каналов (сек)\n    dur_left_s  = ffprobe_duration(left_wav)\n    dur_right_s = ffprobe_duration(right_wav)\n    # RTF: скорость/реалтайм (больше = медленнее). Считаем по отдельности и среднее.\n    rtf_left  = (t_w_ms/1000.0) / (dur_left_s  or 1e-6)\n    rtf_right = (t_w_ms/1000.0) / (dur_right_s or 1e-6)\n    rtf_avg   = (rtf_left + rtf_right) / 2.0"
    )

    s = s.replace(
        "if rcL != 0 or rcR != 0:",
        "if rcL != 0 or rcR != 0:\n        # Попробуем fallback модель, если задана\n        fb = MODEL_PATH_FALLBACK\n        if fb and fb != MODEL_PATH:\n            log(\"whisper failed, retry with fallback model:\", fb)\n            global MODEL_PATH\n            old_model = MODEL_PATH\n            MODEL_PATH = fb\n            _t_w0 = time.time()\n            loop = asyncio.get_running_loop()\n            tL = loop.run_in_executor(None, lambda: whisper_run(left_wav, str(CACHE_DIR / f\"{job_id}_left\"), timeout=TIMEOUT_S))\n            tR = loop.run_in_executor(None, lambda: whisper_run(right_wav, str(CACHE_DIR / f\"{job_id}_right\"), timeout=TIMEOUT_S))\n            (rcL2, outL2, errL2), (rcR2, outR2, errR2) = await asyncio.gather(tL, tR)\n            t_w_ms = int((time.time() - _t_w0) * 1000)\n            # вернуть модель назад для следующих задач\n            MODEL_PATH = old_model\n            if rcL2==0 and rcR2==0:\n                rcL,rcR,errL,errR = rcL2,rcR2,errL2,errR2\n            else:\n                log(\"whisper rcL/rcR =\", rcL2, rcR2)\n                await ws.send_json({\"type\":\"job.error\",\"job_id\":job_id,\"worker_id\":WORKER_ID,\"error\":\"whisper_failed\"})\n                slog(\"EVT:job.error\", {\"job_id\": job_id, \"error\": \"whisper_failed\", \"rcL\": rcL2, \"rcR\": rcR2, \"stderrL_tail\": (errL2 or \"\")[-400:], \"stderrR_tail\": (errR2 or \"\")[-400:]})\n                cleanup_files(mp3_path, left_wav, right_wav)\n                ensure_cache_quota()\n                return\n        else:\n            log(\"whisper rcL/rcR =\", rcL, rcR)\n            await ws.send_json({\"type\":\"job.error\",\"job_id\":job_id,\"worker_id\":WORKER_ID,\"error\":\"whisper_failed\"})\n            slog(\"EVT:job.error\", {\"job_id\": job_id, \"error\": \"whisper_failed\", \"rcL\": rcL, \"rcR\": rcR, \"stderrL_tail\": (errL or \"\")[-400:], \"stderrR_tail\": (errR or \"\")[-400:]})\n            cleanup_files(mp3_path, left_wav, right_wav)\n            ensure_cache_quota()\n            return"
    )

    s = s.replace(
        '"whisper_ms": int(t_w_ms),',
        '"whisper_ms": int(t_w_ms),\n        "mp3_duration_s": mp3_dur,\n        "dur_left_s": dur_left_s,\n        "dur_right_s": dur_right_s,\n        "rtf_left": rtf_left,\n        "rtf_right": rtf_right,\n        "rtf_avg": rtf_avg,'
    )

# 6) heartbeat: добавим секцию job со статусом
if "job\":" not in s or "job_status" not in s:
    s = s.replace(
        '"metrics": m,',
        '"metrics": m,\n            \"job\": (lambda: ({} if CURRENT_JOB is None else {\n                \"id\": CURRENT_JOB.get(\"id\"),\n                \"since_ts\": CURRENT_JOB.get(\"since_ts\"),\n                \"elapsed_s\": int(time.time() - CURRENT_JOB.get(\"since_ts\", time.time())) if CURRENT_JOB.get(\"since_ts\") else None,\n                \"using_gpu\": bool(USE_GPU),\n                \"audio_left_s\": CURRENT_JOB.get(\"audio_left_s\"),\n                \"audio_right_s\": CURRENT_JOB.get(\"audio_right_s\"),\n                \"threads\": THREADS,\n                \"model\": os.path.basename(MODEL_PATH),\n            }))(),'
    )

# 7) в момент старта job заполним CURRENT_JOB, а по завершении — очистим
if "CURRENT_JOB and not CURRENT_JOB.done()" in s and "CURRENT_JOB = {" not in s:
    s = s.replace(
        "async def _run_job():",
        "async def _run_job():\n\n                                    # зафиксируем статус работы для heartbeat\n                                    try:\n                                        left_dur  = None\n                                        right_dur = None\n                                        # мы посчитаем точно после split; предварительно оставим None\n                                        pass\n                                    except Exception:\n                                        pass"
    )

# заполняем CURRENT_JOB сразу после успешного split (знаем длительности WAV)
s = s.replace(
    "t_sp_ms = int((time.time() - _t_sp0) * 1000)\n    if rc != 0:",
    "t_sp_ms = int((time.time() - _t_sp0) * 1000)\n    if rc != 0:"
)
# сразу после split — добавим заполнение CURRENT_JOB (в начале распознавания)
if "CURRENT_JOB = {" not in s:
    s = s.replace(
        "    # параллельное распознавание",
        "    # параллельное распознавание\n    global CURRENT_JOB\n    try:\n        CURRENT_JOB = {\n            \"id\": job_id,\n            \"since_ts\": int(time.time()),\n            \"audio_left_s\": ffprobe_duration(left_wav),\n            \"audio_right_s\": ffprobe_duration(right_wav),\n        }\n    except Exception:\n        CURRENT_JOB = {\"id\": job_id, \"since_ts\": int(time.time())}\n"
    )

s = s.replace(
    "    # уборка и квота",
    "    # уборка и квота\n    CURRENT_JOB = None"
)

# 8) post_result: если 400 — лог уже есть; добавим попытку показать часть ответа в slog
if "POST status" in s and "post_result(session, payload)" in s and "Bad Request" not in s:
    s = s.replace(
        "async with session.post(url, headers=hdrs, data=json.dumps(payload)) as r:",
        "async with session.post(url, headers=hdrs, data=json.dumps(payload)) as r:"
    )  # лог уже выводит text[:500]

p.write_text(s, encoding="utf-8")
print("[OK] Patched agent.py")
