#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
 Raspberry Pi 3 Sistem İzleme Paneli (Production-Ready, Single-File Edition)
=============================================================================

Tek dosyada çalışan, framework KULLANMAYAN (sadece http.server), modern
ve Raspberry Pi 3 (1GB RAM) için optimize edilmiş bir sistem/Docker
izleme paneli.

Özellikler:
    - Koyu temalı (GitHub Dark benzeri), glassmorphism, responsive web arayüzü
    - Gerçek zamanlı (2 sn) otomatik yenilenen sistem metrikleri
    - CPU / RAM / Disk / Sıcaklık / Uptime / OS / Kernel / IP bilgileri
    - Docker container yönetimi (docker inspect + docker stats tabanlı)
    - Otomatik port/servis algılama ve tıklanabilir yerel/Tailscale linkleri
    - Arama kutusu ile container filtreleme
    - Bilinen servisler için otomatik hızlı erişim butonları
    - Telegram bot entegrasyonu (uzun-polling, harici kütüphane yok)
    - REST API: /api/stats, /api/system, /api/docker, /api/network

Gereksinimler:
    - Python 3.7+
    - psutil (pip3 install psutil)
    - (Opsiyonel) docker CLI kurulu ve erişilebilir olmalı
    - (Opsiyonel) tailscale CLI kurulu olmalı
    - (Opsiyonel) Telegram bot için CONFIG["TELEGRAM_BOT_TOKEN"] doldurulmalı

Çalıştırma:
    python3 raspi_monitor.py

Not (reboot/shutdown):
    /reboot ve /shutdown komutlarının çalışabilmesi için servisin root
    yetkisiyle çalıştırılması ya da ilgili kullanıcı için şifresiz sudo
    tanımlanmış olması gerekir.
=============================================================================
"""

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import psutil
except ImportError:
    sys.stderr.write(
        "[HATA] 'psutil' kütüphanesi bulunamadı.\n"
        "Kurulum için: pip3 install psutil\n"
    )
    sys.exit(1)


# =============================================================================
# YAPILANDIRMA (CONFIG)
# =============================================================================
CONFIG = {
    # Web paneli
    "HOST": "0.0.0.0",
    "PORT": 8080,
    "REFRESH_INTERVAL_MS": 2000,      # Panelin yenilenme aralığı (ms)

    # Önbellek (Pi 3 / 1GB RAM için performans optimizasyonu)
    "SYSTEM_CACHE_TTL": 1.0,          # saniye
    "DOCKER_CACHE_TTL": 4.0,          # saniye (docker inspect/stats pahalıdır)

    # Telegram Bot
    "TELEGRAM_BOT_TOKEN": "",         # <-- BURAYA BOT TOKEN'INIZI GİRİN
    "TELEGRAM_CHAT_ID": "",           # Opsiyonel: sadece bu chat id'ye izin ver (boşsa herkes)
    "TELEGRAM_POLL_TIMEOUT": 25,      # long-polling timeout (sn)

    # Güvenlik
    "ALLOW_REBOOT_SHUTDOWN": True,    # /reboot ve /shutdown komutlarına izin ver
}

# Docker image/isimlerinden otomatik servis tespiti için bilinen servisler.
# 'key' -> image/isimde aranacak alt string (küçük harf)
KNOWN_SERVICES = {
    "homepage":     {"label": "Homepage",     "icon": "🏠", "default_port": 3000},
    "portainer":    {"label": "Portainer",    "icon": "🐳", "default_port": 9000},
    "pihole":       {"label": "Pi-hole",      "icon": "🕳️", "default_port": 80},
    "pi-hole":      {"label": "Pi-hole",      "icon": "🕳️", "default_port": 80},
    "nextcloud":    {"label": "Nextcloud",    "icon": "☁️", "default_port": 443},
    "uptime-kuma":  {"label": "Uptime Kuma",  "icon": "📈", "default_port": 3001},
    "uptimekuma":   {"label": "Uptime Kuma",  "icon": "📈", "default_port": 3001},
    "gotify":       {"label": "Gotify",       "icon": "🔔", "default_port": 80},
    "it-tools":     {"label": "IT-Tools",     "icon": "🛠️", "default_port": 80},
    "ittools":      {"label": "IT-Tools",     "icon": "🛠️", "default_port": 80},
    "watchtower":   {"label": "Watchtower",   "icon": "🛰️", "default_port": None},
    "ntfy":         {"label": "Ntfy",         "icon": "📣", "default_port": 80},
    "dozzle":       {"label": "Dozzle",       "icon": "📜", "default_port": 8080},
    "filebrowser":  {"label": "FileBrowser",  "icon": "📁", "default_port": 80},
    "dockge":       {"label": "Dockge",       "icon": "⚙️", "default_port": 5001},
}


# =============================================================================
# BASİT TTL ÖNBELLEK (thread-safe)
# =============================================================================
class TTLCache:
    """Pahalı işlemleri (docker inspect vb.) belirli süre önbellekte tutar."""

    def __init__(self, ttl_seconds):
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._store = {}   # key -> (value, timestamp)

    def get_or_set(self, key, producer_fn):
        now = time.time()
        with self._lock:
            cached = self._store.get(key)
            if cached and (now - cached[1]) < self.ttl:
                return cached[0]
        # Kilidin dışında üret (uzun sürebilecek işlemleri kilitte tutma)
        try:
            value = producer_fn()
        except Exception as exc:  # noqa: BLE001 - panel asla durmamalı
            value = {"error": str(exc)}
        with self._lock:
            self._store[key] = (value, time.time())
        return value


SYSTEM_CACHE = TTLCache(CONFIG["SYSTEM_CACHE_TTL"])
DOCKER_CACHE = TTLCache(CONFIG["DOCKER_CACHE_TTL"])


# =============================================================================
# YARDIMCI FONKSİYONLAR - AĞ / IP
# =============================================================================
def get_local_ip():
    """Yerel IP adresini, gerçek trafik göndermeden soket hilesiyle bulur."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def get_tailscale_ip():
    """Tailscale kuruluysa 'tailscale ip -4' ile IP döndürür, yoksa None."""
    try:
        if not shutil.which("tailscale"):
            return None
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0].strip()
        return None
    except Exception:
        return None


def get_all_network_interfaces():
    """Tüm ağ arayüzlerindeki IPv4 adreslerini listeler."""
    interfaces = {}
    try:
        for iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    interfaces.setdefault(iface, []).append(addr.address)
    except Exception:
        pass
    return interfaces


def get_network_info():
    """Ağ bilgilerini (yerel IP, Tailscale IP, arayüzler) toplar."""
    return {
        "hostname": socket.gethostname(),
        "local_ip": get_local_ip(),
        "tailscale_ip": get_tailscale_ip(),
        "interfaces": get_all_network_interfaces(),
    }


# =============================================================================
# YARDIMCI FONKSİYONLAR - SİSTEM BİLGİLERİ
# =============================================================================
def get_cpu_temperature():
    """CPU sıcaklığını okur (önce thermal_zone, sonra vcgencmd)."""
    try:
        thermal_path = "/sys/class/thermal/thermal_zone0/temp"
        if os.path.exists(thermal_path):
            with open(thermal_path, "r") as f:
                raw = f.read().strip()
            return round(int(raw) / 1000.0, 1)
    except Exception:
        pass
    try:
        if shutil.which("vcgencmd"):
            out = subprocess.run(
                ["vcgencmd", "measure_temp"],
                capture_output=True, text=True, timeout=2,
            )
            match = re.search(r"[\d.]+", out.stdout)
            if match:
                return round(float(match.group()), 1)
    except Exception:
        pass
    return None


def get_uptime_str():
    """Sistem çalışma süresini 'Xg Ys Zd' formatında döndürür."""
    try:
        boot_time = psutil.boot_time()
        delta = timedelta(seconds=int(time.time() - boot_time))
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        parts = []
        if days:
            parts.append(f"{days}g")
        if hours or days:
            parts.append(f"{hours}s")
        parts.append(f"{minutes}d")
        return " ".join(parts)
    except Exception:
        return "N/A"


def get_os_pretty_name():
    """/etc/os-release içinden okunabilir işletim sistemi adını çıkarır."""
    try:
        if os.path.exists("/etc/os-release"):
            data = {}
            with open("/etc/os-release", "r") as f:
                for line in f:
                    if "=" in line:
                        key, _, value = line.strip().partition("=")
                        data[key] = value.strip('"')
            if "PRETTY_NAME" in data:
                return data["PRETTY_NAME"]
        return platform.platform()
    except Exception:
        return platform.platform()


def get_cpu_stats():
    """CPU kullanım yüzdesi, çekirdek sayısı ve frekans bilgisi."""
    try:
        percent = psutil.cpu_percent(interval=0.3)
        try:
            freq = psutil.cpu_freq()
            freq_mhz = round(freq.current, 0) if freq else None
        except Exception:
            freq_mhz = None
        return {
            "percent": percent,
            "cores": psutil.cpu_count(logical=True) or 0,
            "freq_mhz": freq_mhz,
        }
    except Exception:
        return {"percent": 0, "cores": 0, "freq_mhz": None}


def get_ram_stats():
    """RAM kullanım istatistikleri (GB cinsinden)."""
    try:
        mem = psutil.virtual_memory()
        return {
            "percent": mem.percent,
            "total_gb": round(mem.total / (1024 ** 3), 2),
            "used_gb": round(mem.used / (1024 ** 3), 2),
            "available_gb": round(mem.available / (1024 ** 3), 2),
        }
    except Exception:
        return {"percent": 0, "total_gb": 0, "used_gb": 0, "available_gb": 0}


def get_disk_stats():
    """Kök disk kullanım istatistikleri (GB cinsinden)."""
    try:
        disk = psutil.disk_usage("/")
        return {
            "percent": disk.percent,
            "total_gb": round(disk.total / (1024 ** 3), 2),
            "used_gb": round(disk.used / (1024 ** 3), 2),
            "free_gb": round(disk.free / (1024 ** 3), 2),
        }
    except Exception:
        return {"percent": 0, "total_gb": 0, "used_gb": 0, "free_gb": 0}


def get_system_stats():
    """Tüm sistem metriklerini tek bir sözlükte toplar."""
    return {
        "hostname": socket.gethostname(),
        "os": get_os_pretty_name(),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "cpu": get_cpu_stats(),
        "ram": get_ram_stats(),
        "disk": get_disk_stats(),
        "temperature": get_cpu_temperature(),
        "uptime": get_uptime_str(),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# =============================================================================
# DOCKER YÖNETİMİ
# =============================================================================
def is_docker_available():
    """Docker CLI'nin sistemde kurulu olup olmadığını kontrol eder."""
    return shutil.which("docker") is not None


def _docker_list_container_ids():
    """Tüm (çalışan + durdurulmuş) container ID'lerini döndürür."""
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "-q"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.strip().splitlines() if line]
    except Exception:
        return []


def _docker_inspect(container_ids):
    """Verilen container ID'leri için 'docker inspect' çıktısını JSON olarak döndürür."""
    if not container_ids:
        return []
    try:
        result = subprocess.run(
            ["docker", "inspect"] + container_ids,
            capture_output=True, text=True, timeout=8,
        )
        if result.returncode != 0:
            return []
        return json.loads(result.stdout)
    except Exception:
        return []


def _docker_stats_all():
    """'docker stats --no-stream' ile tüm container'ların CPU/RAM kullanımını tek seferde alır."""
    stats_by_id = {}
    try:
        fmt = "{{.ID}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}"
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", fmt],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                parts = line.split("|")
                if len(parts) >= 4:
                    cid, cpu, mem_usage, mem_perc = parts[0], parts[1], parts[2], parts[3]
                    stats_by_id[cid.strip()] = {
                        "cpu": cpu.strip(),
                        "mem_usage": mem_usage.strip(),
                        "mem_perc": mem_perc.strip(),
                    }
    except Exception:
        pass
    return stats_by_id


def _detect_known_service(name, image):
    """Container adı/imajına bakarak bilinen servis eşleşmesi arar."""
    haystack = f"{name} {image}".lower()
    for key, meta in KNOWN_SERVICES.items():
        if key in haystack:
            return meta
    return None


def _extract_ports(inspect_item):
    """Container'ın açık portlarını (container_port -> host_port) çıkarır."""
    ports = []
    try:
        port_bindings = (inspect_item.get("NetworkSettings", {}) or {}).get("Ports") or {}
        for container_port, bindings in port_bindings.items():
            if not bindings:
                continue
            for binding in bindings:
                host_port = binding.get("HostPort")
                if host_port:
                    ports.append({
                        "container_port": container_port,
                        "host_port": host_port,
                    })
    except Exception:
        pass
    return ports


def _build_access_links(host_port, local_ip, tailscale_ip):
    """Bir port için yerel ve Tailscale erişim linklerini üretir."""
    links = []
    if tailscale_ip:
        links.append({"type": "Tailscale", "url": f"http://{tailscale_ip}:{host_port}"})
    if local_ip:
        links.append({"type": "Yerel", "url": f"http://{local_ip}:{host_port}"})
    return links


def get_docker_containers():
    """Tüm Docker container'larının detaylı bilgilerini toplar."""
    if not is_docker_available():
        return {"available": False, "containers": [], "message": "Docker kurulu değil veya erişilemiyor."}

    try:
        container_ids = _docker_list_container_ids()
        inspects = _docker_inspect(container_ids)
        stats_map = _docker_stats_all()
        local_ip = get_local_ip()
        tailscale_ip = get_tailscale_ip()

        containers = []
        for item in inspects:
            try:
                name = item.get("Name", "").lstrip("/")
                state = item.get("State", {}) or {}
                status = state.get("Status", "unknown")
                running = bool(state.get("Running", False))
                config = item.get("Config", {}) or {}
                image = config.get("Image", "unknown")
                host_config = item.get("HostConfig", {}) or {}
                restart_policy = (host_config.get("RestartPolicy", {}) or {}).get("Name", "no") or "no"

                short_id = (item.get("Id", "") or "")[:12]
                stat = stats_map.get(short_id, {"cpu": "N/A", "mem_usage": "N/A", "mem_perc": "N/A"})

                ports = _extract_ports(item)
                for p in ports:
                    p["links"] = _build_access_links(p["host_port"], local_ip, tailscale_ip)

                known_service = _detect_known_service(name, image)

                containers.append({
                    "id": short_id,
                    "name": name,
                    "status": status,
                    "running": running,
                    "image": image,
                    "restart_policy": restart_policy,
                    "cpu": stat["cpu"],
                    "mem_usage": stat["mem_usage"],
                    "mem_perc": stat["mem_perc"],
                    "ports": ports,
                    "service_label": known_service["label"] if known_service else None,
                    "service_icon": known_service["icon"] if known_service else "📦",
                })
            except Exception:
                # Tek bir container'da hata olsa bile diğerleri gösterilmeye devam etsin
                continue

        # Çalışanlar üstte olacak şekilde sırala
        containers.sort(key=lambda c: (not c["running"], c["name"].lower()))

        return {"available": True, "containers": containers}
    except Exception as exc:
        return {"available": False, "containers": [], "message": str(exc)}


# =============================================================================
# TÜM VERİYİ TOPLAYAN ANA FONKSİYON (API için)
# =============================================================================
def build_full_stats():
    """/api/stats için sistem + ağ + docker verisini birleştirir."""
    return {
        "system": SYSTEM_CACHE.get_or_set("system", get_system_stats),
        "network": SYSTEM_CACHE.get_or_set("network", get_network_info),
        "docker": DOCKER_CACHE.get_or_set("docker", get_docker_containers),
    }


# =============================================================================
# TELEGRAM BOT
# =============================================================================
def telegram_api_call(method, params=None, timeout=30):
    """Telegram Bot API'ye stdlib (urllib) kullanarak istek atar."""
    token = CONFIG["TELEGRAM_BOT_TOKEN"]
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        data = urllib.parse.urlencode(params or {}).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def telegram_send_message(chat_id, text):
    """Telegram'a Markdown destekli mesaj gönderir."""
    return telegram_api_call("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    })


def _fmt_status_md():
    s = get_system_stats()
    temp = f"{s['temperature']}°C" if s["temperature"] is not None else "N/A"
    return (
        "*📊 Sistem Durumu*\n"
        f"🖥 Hostname: `{s['hostname']}`\n"
        f"🐧 OS: `{s['os']}`\n"
        f"⚙️ Kernel: `{s['kernel']}`\n"
        f"🔲 CPU: `%{s['cpu']['percent']}`\n"
        f"🧠 RAM: `%{s['ram']['percent']}` ({s['ram']['used_gb']}GB / {s['ram']['total_gb']}GB)\n"
        f"💾 Disk: `%{s['disk']['percent']}` ({s['disk']['used_gb']}GB / {s['disk']['total_gb']}GB)\n"
        f"🌡 Sıcaklık: `{temp}`\n"
        f"⏱ Uptime: `{s['uptime']}`"
    )


def _fmt_cpu_md():
    c = get_cpu_stats()
    return f"*⚙️ CPU Kullanımı*\n`%{c['percent']}` — {c['cores']} çekirdek"


def _fmt_ram_md():
    r = get_ram_stats()
    return f"*🧠 RAM Kullanımı*\n`%{r['percent']}` ({r['used_gb']}GB / {r['total_gb']}GB)"


def _fmt_disk_md():
    d = get_disk_stats()
    return f"*💾 Disk Kullanımı*\n`%{d['percent']}` ({d['used_gb']}GB / {d['total_gb']}GB, boş: {d['free_gb']}GB)"


def _fmt_temp_md():
    t = get_cpu_temperature()
    return f"*🌡 CPU Sıcaklığı*\n`{t}°C`" if t is not None else "*🌡 CPU Sıcaklığı*\n`N/A`"


def _fmt_docker_md():
    data = get_docker_containers()
    if not data.get("available"):
        return "*🐳 Docker*\nDocker kurulu değil veya erişilemiyor."
    containers = data.get("containers", [])
    if not containers:
        return "*🐳 Docker*\nHiç container bulunamadı."
    lines = ["*🐳 Docker Container'ları*"]
    for c in containers:
        badge = "🟢" if c["running"] else "🔴"
        lines.append(f"{badge} `{c['name']}` — {c['status']} (CPU: {c['cpu']}, RAM: {c['mem_usage']})")
    return "\n".join(lines)


def _handle_reboot():
    if not CONFIG["ALLOW_REBOOT_SHUTDOWN"]:
        return "⛔ Reboot komutu devre dışı bırakılmış."
    try:
        subprocess.Popen(["sudo", "reboot"])
        return "🔄 Sistem yeniden başlatılıyor..."
    except Exception as exc:
        return f"⚠️ Reboot başarısız: {exc}"


def _handle_shutdown():
    if not CONFIG["ALLOW_REBOOT_SHUTDOWN"]:
        return "⛔ Shutdown komutu devre dışı bırakılmış."
    try:
        subprocess.Popen(["sudo", "shutdown", "-h", "now"])
        return "⏻ Sistem kapatılıyor..."
    except Exception as exc:
        return f"⚠️ Shutdown başarısız: {exc}"


TELEGRAM_COMMANDS = {
    "/start": lambda: (
        "👋 *Raspberry Pi Monitor Bot'a hoş geldiniz!*\n\n"
        "Kullanılabilir komutlar:\n"
        "/durum - Genel sistem durumu\n"
        "/docker - Docker container'ları\n"
        "/containers - Container listesi\n"
        "/cpu - CPU kullanımı\n"
        "/ram - RAM kullanımı\n"
        "/disk - Disk kullanımı\n"
        "/temp - CPU sıcaklığı\n"
        "/reboot - Sistemi yeniden başlat\n"
        "/shutdown - Sistemi kapat"
    ),
    "/durum": _fmt_status_md,
    "/docker": _fmt_docker_md,
    "/containers": _fmt_docker_md,
    "/cpu": _fmt_cpu_md,
    "/ram": _fmt_ram_md,
    "/disk": _fmt_disk_md,
    "/temp": _fmt_temp_md,
    "/reboot": _handle_reboot,
    "/shutdown": _handle_shutdown,
}


def handle_telegram_update(update):
    """Tek bir Telegram güncellemesini (mesajını) işler."""
    try:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        chat_id = message.get("chat", {}).get("id")
        text = (message.get("text") or "").strip()

        allowed_chat = CONFIG["TELEGRAM_CHAT_ID"]
        if allowed_chat and str(chat_id) != str(allowed_chat):
            return  # Whitelist dışı sohbetleri sessizce yok say

        command = text.split()[0].lower() if text else ""
        handler = TELEGRAM_COMMANDS.get(command)
        if handler:
            reply = handler()
        else:
            reply = "❓ Bilinmeyen komut. Komut listesi için /start yazın."

        telegram_send_message(chat_id, reply)
    except Exception:
        pass  # Bot thread'i asla çökmemeli


def telegram_bot_loop():
    """Telegram uzun-polling (long-polling) döngüsü. Ayrı bir thread'de çalışır."""
    if not CONFIG["TELEGRAM_BOT_TOKEN"]:
        return  # Token tanımlı değilse bot devre dışı

    offset = 0
    while True:
        try:
            response = telegram_api_call(
                "getUpdates",
                {"offset": offset, "timeout": CONFIG["TELEGRAM_POLL_TIMEOUT"]},
                timeout=CONFIG["TELEGRAM_POLL_TIMEOUT"] + 10,
            )
            if not response or not response.get("ok"):
                time.sleep(2)
                continue
            for update in response.get("result", []):
                offset = update["update_id"] + 1
                handle_telegram_update(update)
        except Exception:
            time.sleep(3)  # Ağ hatası vb. durumda paneli etkilemeden devam et


# =============================================================================
# WEB ARAYÜZÜ (HTML + CSS + JS - TEK STRING)
# =============================================================================
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Raspberry Pi Monitor</title>
<style>
    :root {
        --bg-primary: #0d1117;
        --bg-secondary: #161b22;
        --bg-card: rgba(22, 27, 34, 0.65);
        --border-color: rgba(240, 246, 252, 0.1);
        --text-primary: #c9d1d9;
        --text-secondary: #8b949e;
        --accent-blue: #58a6ff;
        --accent-green: #3fb950;
        --accent-red: #f85149;
        --accent-yellow: #d29922;
        --accent-purple: #bc8cff;
        --shadow-glass: 0 8px 32px rgba(0, 0, 0, 0.4);
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        background: radial-gradient(circle at 20% 0%, #161f2c 0%, #0d1117 55%);
        color: var(--text-primary);
        min-height: 100vh;
        padding: 16px;
        line-height: 1.5;
    }

    .container { max-width: 1280px; margin: 0 auto; }

    /* ---------- Header ---------- */
    header {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 24px;
        animation: fadeInDown 0.5s ease;
    }

    .brand { display: flex; align-items: center; gap: 10px; }
    .brand .emoji { font-size: 28px; }
    .brand h1 { font-size: 20px; font-weight: 600; }
    .brand .hostname { color: var(--text-secondary); font-size: 13px; }

    .search-box {
        flex: 1;
        min-width: 200px;
        max-width: 340px;
        position: relative;
    }
    .search-box input {
        width: 100%;
        padding: 10px 14px 10px 36px;
        border-radius: 10px;
        border: 1px solid var(--border-color);
        background: var(--bg-card);
        backdrop-filter: blur(10px);
        color: var(--text-primary);
        font-size: 14px;
        outline: none;
        transition: border-color 0.2s ease;
    }
    .search-box input:focus { border-color: var(--accent-blue); }
    .search-box::before {
        content: "🔍";
        position: absolute;
        left: 12px;
        top: 50%;
        transform: translateY(-50%);
        font-size: 13px;
        opacity: 0.7;
    }

    .live-indicator {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        color: var(--text-secondary);
    }
    .live-dot {
        width: 8px; height: 8px; border-radius: 50%;
        background: var(--accent-green);
        box-shadow: 0 0 8px var(--accent-green);
        animation: pulse 1.6s infinite;
    }

    /* ---------- Glass Card ---------- */
    .glass-card {
        background: var(--bg-card);
        border: 1px solid var(--border-color);
        border-radius: 14px;
        backdrop-filter: blur(12px);
        -webkit-backdrop-filter: blur(12px);
        box-shadow: var(--shadow-glass);
        padding: 16px;
        animation: fadeInUp 0.45s ease;
        transition: transform 0.2s ease, border-color 0.2s ease;
    }
    .glass-card:hover { transform: translateY(-2px); border-color: rgba(88,166,255,0.35); }

    /* ---------- Section titles ---------- */
    .section-title {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 15px;
        font-weight: 600;
        margin: 28px 0 12px;
        color: var(--text-primary);
    }
    .section-title .count-badge {
        background: rgba(88,166,255,0.15);
        color: var(--accent-blue);
        font-size: 12px;
        padding: 2px 8px;
        border-radius: 20px;
        font-weight: 500;
    }

    /* ---------- System grid ---------- */
    .grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
        gap: 14px;
    }

    .stat-card .stat-label {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        color: var(--text-secondary);
        text-transform: uppercase;
        letter-spacing: 0.4px;
        margin-bottom: 8px;
    }
    .stat-card .stat-value {
        font-size: 24px;
        font-weight: 700;
    }
    .stat-card .stat-sub {
        font-size: 12px;
        color: var(--text-secondary);
        margin-top: 4px;
    }

    .progress-track {
        margin-top: 10px;
        height: 6px;
        border-radius: 6px;
        background: rgba(255,255,255,0.08);
        overflow: hidden;
    }
    .progress-fill {
        height: 100%;
        border-radius: 6px;
        transition: width 0.6s ease, background-color 0.6s ease;
    }

    .info-card .info-row {
        display: flex;
        justify-content: space-between;
        font-size: 13px;
        padding: 6px 0;
        border-bottom: 1px dashed var(--border-color);
    }
    .info-card .info-row:last-child { border-bottom: none; }
    .info-card .info-row span:first-child { color: var(--text-secondary); }
    .info-card .info-row span:last-child { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }

    /* ---------- Services quick launch ---------- */
    .services-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
        gap: 12px;
    }
    .service-btn {
        display: flex;
        flex-direction: column;
        gap: 6px;
        padding: 14px;
        border-radius: 12px;
        text-decoration: none;
        color: var(--text-primary);
        background: var(--bg-card);
        border: 1px solid var(--border-color);
        transition: all 0.2s ease;
    }
    .service-btn:hover {
        border-color: var(--accent-blue);
        transform: translateY(-2px);
        box-shadow: 0 6px 20px rgba(88,166,255,0.2);
    }
    .service-btn .svc-name { font-weight: 600; font-size: 14px; display: flex; align-items: center; gap: 8px; }
    .service-btn .svc-link { font-size: 11px; color: var(--text-secondary); word-break: break-all; }
    .empty-hint { color: var(--text-secondary); font-size: 13px; padding: 12px 0; }

    /* ---------- Docker container cards ---------- */
    .containers-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
        gap: 14px;
    }
    .container-card {
        cursor: pointer;
        position: relative;
        overflow: hidden;
    }
    .container-card .c-header {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 8px;
    }
    .container-card .c-name {
        font-weight: 600;
        font-size: 15px;
        display: flex;
        align-items: center;
        gap: 6px;
    }
    .badge {
        font-size: 11px;
        font-weight: 600;
        padding: 3px 9px;
        border-radius: 20px;
        white-space: nowrap;
    }
    .badge.running { background: rgba(63,185,80,0.15); color: var(--accent-green); }
    .badge.stopped { background: rgba(248,81,73,0.15); color: var(--accent-red); }

    .container-card .c-image {
        font-size: 12px;
        color: var(--text-secondary);
        margin: 8px 0;
        word-break: break-all;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    .c-metrics {
        display: flex;
        gap: 16px;
        font-size: 12px;
        color: var(--text-secondary);
        margin-bottom: 6px;
    }
    .c-metrics b { color: var(--text-primary); }

    .c-detail {
        max-height: 0;
        opacity: 0;
        overflow: hidden;
        transition: max-height 0.35s ease, opacity 0.3s ease, margin-top 0.35s ease;
    }
    .container-card.expanded .c-detail {
        max-height: 500px;
        opacity: 1;
        margin-top: 10px;
        border-top: 1px solid var(--border-color);
        padding-top: 10px;
    }
    .c-detail .detail-row {
        display: flex;
        justify-content: space-between;
        font-size: 12px;
        padding: 4px 0;
    }
    .c-detail .detail-row span:first-child { color: var(--text-secondary); }

    .port-links { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
    .port-links a {
        font-size: 11px;
        padding: 4px 8px;
        border-radius: 6px;
        background: rgba(88,166,255,0.12);
        color: var(--accent-blue);
        text-decoration: none;
        border: 1px solid rgba(88,166,255,0.25);
        transition: background 0.2s ease;
    }
    .port-links a:hover { background: rgba(88,166,255,0.25); }

    footer {
        text-align: center;
        color: var(--text-secondary);
        font-size: 12px;
        margin: 32px 0 12px;
    }

    /* ---------- Animations ---------- */
    @keyframes fadeInUp {
        from { opacity: 0; transform: translateY(10px); }
        to { opacity: 1; transform: translateY(0); }
    }
    @keyframes fadeInDown {
        from { opacity: 0; transform: translateY(-10px); }
        to { opacity: 1; transform: translateY(0); }
    }
    @keyframes pulse {
        0% { box-shadow: 0 0 0 0 rgba(63,185,80,0.6); }
        70% { box-shadow: 0 0 0 6px rgba(63,185,80,0); }
        100% { box-shadow: 0 0 0 0 rgba(63,185,80,0); }
    }

    @media (max-width: 640px) {
        .brand h1 { font-size: 17px; }
        .stat-card .stat-value { font-size: 20px; }
        header { flex-direction: column; align-items: flex-start; }
        .search-box { max-width: 100%; }
    }
</style>
</head>
<body>
<div class="container">

    <header>
        <div class="brand">
            <span class="emoji">🍓</span>
            <div>
                <h1>Raspberry Pi Monitor</h1>
                <div class="hostname" id="hostnameLabel">—</div>
            </div>
        </div>
        <div class="search-box">
            <input type="text" id="searchInput" placeholder="Container ara...">
        </div>
        <div class="live-indicator">
            <span class="live-dot"></span>
            <span id="lastUpdate">Yükleniyor...</span>
        </div>
    </header>

    <!-- Sistem Bilgileri -->
    <div class="section-title">📊 Sistem Bilgileri</div>
    <div class="grid" id="systemGrid"></div>

    <!-- Hızlı Erişim Servisleri -->
    <div class="section-title">🚀 Servisler <span class="count-badge" id="servicesCount">0</span></div>
    <div class="services-grid" id="servicesGrid"></div>

    <!-- Docker Container'ları -->
    <div class="section-title">🐳 Docker Container'ları <span class="count-badge" id="containersCount">0</span></div>
    <div class="containers-grid" id="containersGrid"></div>

    <footer>Otomatik yenileme: her __REFRESH_MS__ ms &middot; Raspberry Pi 3 için optimize edilmiştir</footer>
</div>

<script>
(function () {
    "use strict";

    const REFRESH_MS = __REFRESH_MS__;
    let lastDockerData = { containers: [] };
    let searchTerm = "";

    // ---------------- Yardımcı formatlayıcılar ----------------
    function progressColor(percent) {
        if (percent >= 85) return "var(--accent-red)";
        if (percent >= 60) return "var(--accent-yellow)";
        return "var(--accent-green)";
    }

    function escapeHtml(str) {
        if (str === null || str === undefined) return "";
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    // ---------------- Render: Sistem kartları ----------------
    function renderSystem(system, network) {
        if (!system || system.error) return;

        document.getElementById("hostnameLabel").textContent =
            (system.hostname || "—") + " · " + (system.os || "");

        const cpu = system.cpu || {};
        const ram = system.ram || {};
        const disk = system.disk || {};
        const temp = system.temperature;

        const cards = [];

        cards.push(makeStatCard("⚙️ CPU", (cpu.percent ?? 0) + "%",
            cpu.cores + " çekirdek" + (cpu.freq_mhz ? " · " + cpu.freq_mhz + " MHz" : ""),
            cpu.percent));

        cards.push(makeStatCard("🧠 RAM", (ram.percent ?? 0) + "%",
            ram.used_gb + " GB / " + ram.total_gb + " GB",
            ram.percent));

        cards.push(makeStatCard("💾 Disk", (disk.percent ?? 0) + "%",
            disk.used_gb + " GB / " + disk.total_gb + " GB (boş: " + disk.free_gb + " GB)",
            disk.percent));

        cards.push(makeStatCard("🌡️ Sıcaklık", (temp !== null && temp !== undefined) ? temp + "°C" : "N/A",
            "CPU sıcaklığı", temp || 0));

        cards.push(makeInfoCard("🖥️ Sistem", [
            ["Hostname", system.hostname],
            ["OS", system.os],
            ["Kernel", system.kernel],
            ["Uptime", system.uptime],
        ]));

        const net = network || {};
        cards.push(makeInfoCard("🌐 Ağ", [
            ["Yerel IP", net.local_ip || "N/A"],
            ["Tailscale IP", net.tailscale_ip || "Kurulu değil"],
        ]));

        document.getElementById("systemGrid").innerHTML = cards.join("");
    }

    function makeStatCard(label, value, sub, percent) {
        const p = Math.max(0, Math.min(100, Number(percent) || 0));
        return (
            '<div class="glass-card stat-card">' +
                '<div class="stat-label">' + label + '</div>' +
                '<div class="stat-value">' + value + '</div>' +
                '<div class="stat-sub">' + escapeHtml(sub) + '</div>' +
                '<div class="progress-track"><div class="progress-fill" style="width:' + p + '%;background:' + progressColor(p) + '"></div></div>' +
            '</div>'
        );
    }

    function makeInfoCard(title, rows) {
        const rowsHtml = rows.map(function (r) {
            return '<div class="info-row"><span>' + escapeHtml(r[0]) + '</span><span>' + escapeHtml(r[1]) + '</span></div>';
        }).join("");
        return '<div class="glass-card info-card"><div class="stat-label">' + title + '</div>' + rowsHtml + '</div>';
    }

    // ---------------- Render: Servis butonları ----------------
    function renderServices(containers) {
        const known = containers.filter(function (c) { return c.service_label && c.ports && c.ports.length; });
        document.getElementById("servicesCount").textContent = known.length;

        if (!known.length) {
            document.getElementById("servicesGrid").innerHTML =
                '<div class="empty-hint">Bilinen bir servis algılanmadı.</div>';
            return;
        }

        const html = known.map(function (c) {
            const port = c.ports[0];
            const link = (port.links && port.links[0]) ? port.links[0].url : "#";
            return (
                '<a class="service-btn" href="' + link + '" target="_blank" rel="noopener">' +
                    '<div class="svc-name">' + (c.service_icon || "📦") + " " + escapeHtml(c.service_label) + '</div>' +
                    '<div class="svc-link">' + escapeHtml(link) + '</div>' +
                '</a>'
            );
        }).join("");
        document.getElementById("servicesGrid").innerHTML = html;
    }

    // ---------------- Render: Docker container kartları ----------------
    function renderContainers(dockerData) {
        lastDockerData = dockerData || { containers: [] };
        const containers = lastDockerData.containers || [];
        document.getElementById("containersCount").textContent = containers.length;

        if (!lastDockerData.available) {
            document.getElementById("containersGrid").innerHTML =
                '<div class="empty-hint">' + escapeHtml(lastDockerData.message || "Docker kullanılamıyor.") + '</div>';
            renderServices([]);
            return;
        }

        if (!containers.length) {
            document.getElementById("containersGrid").innerHTML =
                '<div class="empty-hint">Hiç container bulunamadı.</div>';
            renderServices([]);
            return;
        }

        const filtered = containers.filter(function (c) {
            return c.name.toLowerCase().indexOf(searchTerm) !== -1;
        });

        const html = filtered.map(function (c, idx) {
            const badgeClass = c.running ? "running" : "stopped";
            const badgeText = c.running ? "Çalışıyor" : "Durdu";

            const portsHtml = (c.ports || []).map(function (p) {
                const links = (p.links || []).map(function (l) {
                    return '<a href="' + l.url + '" target="_blank" rel="noopener">' + l.type + ': ' + l.url + '</a>';
                }).join("");
                return '<div style="margin-bottom:4px;font-size:12px;color:var(--text-secondary)">Port ' + p.container_port + ' → ' + p.host_port + '</div><div class="port-links">' + links + '</div>';
            }).join("");

            return (
                '<div class="glass-card container-card" data-name="' + escapeHtml(c.name.toLowerCase()) + '" onclick="window.__toggleContainer(' + idx + ')">' +
                    '<div class="c-header">' +
                        '<div class="c-name">' + (c.service_icon || "📦") + ' ' + escapeHtml(c.name) + '</div>' +
                        '<span class="badge ' + badgeClass + '">' + badgeText + '</span>' +
                    '</div>' +
                    '<div class="c-image">' + escapeHtml(c.image) + '</div>' +
                    '<div class="c-metrics">' +
                        '<span>CPU: <b>' + escapeHtml(c.cpu) + '</b></span>' +
                        '<span>RAM: <b>' + escapeHtml(c.mem_usage) + '</b></span>' +
                    '</div>' +
                    '<div class="c-detail">' +
                        '<div class="detail-row"><span>Durum</span><span>' + escapeHtml(c.status) + '</span></div>' +
                        '<div class="detail-row"><span>ID</span><span>' + escapeHtml(c.id) + '</span></div>' +
                        '<div class="detail-row"><span>Restart Policy</span><span>' + escapeHtml(c.restart_policy) + '</span></div>' +
                        '<div class="detail-row"><span>RAM %</span><span>' + escapeHtml(c.mem_perc) + '</span></div>' +
                        (portsHtml || '<div style="font-size:12px;color:var(--text-secondary);margin-top:6px;">Açık port yok</div>') +
                    '</div>' +
                '</div>'
            );
        }).join("");

        document.getElementById("containersGrid").innerHTML = html ||
            '<div class="empty-hint">Aramanızla eşleşen container bulunamadı.</div>';

        renderServices(containers);
    }

    window.__toggleContainer = function (idx) {
        const cards = document.querySelectorAll(".container-card");
        if (cards[idx]) cards[idx].classList.toggle("expanded");
    };

    // ---------------- Arama kutusu ----------------
    document.getElementById("searchInput").addEventListener("input", function (e) {
        searchTerm = e.target.value.trim().toLowerCase();
        renderContainers(lastDockerData);
    });

    // ---------------- Veri çekme döngüsü ----------------
    function fetchStats() {
        fetch("/api/stats")
            .then(function (res) { return res.json(); })
            .then(function (data) {
                renderSystem(data.system, data.network);
                renderContainers(data.docker);
                const now = new Date();
                document.getElementById("lastUpdate").textContent =
                    "Güncellendi: " + now.toLocaleTimeString("tr-TR");
            })
            .catch(function () {
                document.getElementById("lastUpdate").textContent = "Bağlantı hatası, tekrar denenecek...";
            });
    }

    fetchStats();
    setInterval(fetchStats, REFRESH_MS);
})();
</script>
</body>
</html>
"""

DASHBOARD_HTML = DASHBOARD_HTML.replace("__REFRESH_MS__", str(CONFIG["REFRESH_INTERVAL_MS"]))


# =============================================================================
# HTTP SUNUCUSU
# =============================================================================
class MonitorRequestHandler(BaseHTTPRequestHandler):
    """Panel HTML'ini ve REST API endpoint'lerini sunan HTTP handler."""

    server_version = "RaspiMonitor/1.0"

    def log_message(self, log_format, *args):
        # Konsolu gereksiz erişim loglarıyla doldurmamak için susturulur.
        pass

    def _send_json(self, payload, status=200):
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass  # Yazma sırasında bağlantı koparsa panel çökmemeli

    def _send_html(self, html):
        try:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path in ("/", "/index.html"):
                self._send_html(DASHBOARD_HTML)
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            elif path == "/api/stats":
                self._send_json(build_full_stats())
            elif path == "/api/system":
                self._send_json(SYSTEM_CACHE.get_or_set("system", get_system_stats))
            elif path == "/api/docker":
                self._send_json(DOCKER_CACHE.get_or_set("docker", get_docker_containers))
            elif path == "/api/network":
                self._send_json(SYSTEM_CACHE.get_or_set("network", get_network_info))
            else:
                self._send_json({"error": "Not Found", "path": path}, status=404)
        except Exception as exc:
            # Panel hiçbir koşulda tamamen durmamalı
            try:
                self._send_json({"error": str(exc)}, status=500)
            except Exception:
                pass


# =============================================================================
# ANA PROGRAM
# =============================================================================
def print_startup_banner():
    """Başlangıçta konsola erişim bilgilerini yazdırır."""
    local_ip = get_local_ip()
    tailscale_ip = get_tailscale_ip()
    port = CONFIG["PORT"]

    print("=" * 60)
    print(" 🍓 Raspberry Pi Monitor başlatıldı")
    print("=" * 60)
    print(f" Yerel erişim     : http://{local_ip}:{port}")
    if tailscale_ip:
        print(f" Tailscale erişim : http://{tailscale_ip}:{port}")
    else:
        print(" Tailscale        : kurulu değil / bulunamadı")
    print(f" Docker           : {'mevcut' if is_docker_available() else 'bulunamadı'}")
    print(f" Telegram Bot     : {'aktif' if CONFIG['TELEGRAM_BOT_TOKEN'] else 'devre dışı (token boş)'}")
    print("=" * 60)


def main():
    """Panelin ve (varsa) Telegram bot thread'inin başlatıldığı ana giriş noktası."""
    print_startup_banner()

    # Telegram bot arka planda, ana paneli bloklamadan çalışsın
    if CONFIG["TELEGRAM_BOT_TOKEN"]:
        bot_thread = threading.Thread(target=telegram_bot_loop, daemon=True)
        bot_thread.start()

    try:
        server = ThreadingHTTPServer((CONFIG["HOST"], CONFIG["PORT"]), MonitorRequestHandler)
    except Exception as exc:
        sys.stderr.write(f"[HATA] Sunucu başlatılamadı: {exc}\n")
        sys.exit(1)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[i] Kapatma sinyali alındı, panel durduruluyor...")
    except Exception as exc:
        sys.stderr.write(f"[HATA] Beklenmeyen sunucu hatası: {exc}\n")
    finally:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
