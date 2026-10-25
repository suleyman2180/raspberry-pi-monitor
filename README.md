Raspberry Pi Monitor 🍓

Lightweight single-file Raspberry Pi system and Docker monitoring dashboard.

Özellikler

• CPU / RAM / Disk izleme
• CPU sıcaklığı
• Uptime bilgisi
• Docker container listesi
• Container portlarını algılama
• Yerel IP ve Tailscale IP desteği
• Telegram bot desteği
• REST API
• Raspberry Pi 3 için optimize edildi

Kurulum

bash
pip3 install -r requirements.txt
python3 raspi_monitor.py

## Güvenlik notları

- Web paneli varsayılan olarak yalnızca bu cihazdan erişilebilir: `127.0.0.1:8080`. Ağa doğrudan açmak yerine VPN/Tailscale üzerinden güvenli erişim veya kimlik doğrulamalı bir reverse proxy kullanın.
- API, sistem ve ağ bilgilerini içerir; bu yüzden herkese açık ağa yayınlamayın.
- Telegram botunu kullanacaksanız `TELEGRAM_CHAT_ID` alanına kendi izinli sohbet kimliğinizi yazın. Boş bırakılırsa bot komutları hiçbir sohbet için çalıştırılmaz.
- `ALLOW_REBOOT_SHUTDOWN` varsayılan olarak `False` değerindedir. Yeniden başlatma/kapatma komutlarını yalnızca gerekli olduğunda, izinli sohbet doğrulamasını yaptıktan sonra açın.
- Paneli root olarak çalıştırmayın. Gereken sistem komutlarına mümkün olduğunca dar kapsamlı sudo izinleri verin.
