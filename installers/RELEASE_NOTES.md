## 0.2.0

- Mütalaa hesabı, giriş, cihaz onayı ve koşul kabulü kaldırıldı. Kurulumdan sonra araçlar doğrudan çalışır; `mutalaamcp auth` komutları artık yoktur.
- Yerel HTTP bağlantısı (Codex) OAuth olmadan çalışır. Mevcut `http://127.0.0.1:18769/mcp` ayarı aynen geçerlidir.
- Araç sonuçlarına eklenen Mütalaa duyuruları kaldırıldı.
- Eski sürümden kalan oturum bilgileri (anahtar zincirindeki belirteç, `auth_state.json`, yerel OAuth ve duyuru kayıtları) ilk açılışta bir kez silinir.
- Stdio kullanıcıları yeni sürüme otomatik geçmez; Mütalaa'ya bağlı uygulamaları kapatıp `mutalaamcp update` çalıştırın.

### Changes in English

- Mütalaa account sign-in, device approval and terms acceptance are removed. Tools work right after installation; the `mutalaamcp auth` commands no longer exist.
- The local HTTP connection (Codex) works without OAuth. The existing `http://127.0.0.1:18769/mcp` setting stays valid.
- Mütalaa announcements attached to tool results are removed.
- Session data left by older versions (the keychain token, `auth_state.json`, local OAuth and announcement records) is deleted once on first start.
- stdio users are not updated automatically; close the apps connected to Mütalaa and run `mutalaamcp update`.

## Kurulum / Install

[Terminalden kurulum rehberi](https://github.com/hburaktasyurek/MutalaaMCP/blob/main/docs/client-setup/README.md) ile bu sürümü kurabilirsiniz. Python ortamını uv hazırlar.

- **macOS:** Terminal rehberini kullanın.
- **Windows:** Terminal rehberi önerilir. `Mutalaa-Kur.cmd` deneysel bir alternatiftir; indirme, ilk açılış ve hizmet kurulumu gerçek cihazda doğrulanmamıştır.
- **Linux:** Kurulum desteği henüz doğrulanmamıştır.

`SHA256SUMS.txt`, indirilen dosyaların bütünlüğünü kontrol etmek içindir.

### English

Follow the [terminal installation guide](https://github.com/hburaktasyurek/MutalaaMCP/blob/main/docs/client-setup/README.en.md). uv prepares the Python environment.

- **macOS:** Use the terminal guide.
- **Windows:** The terminal guide is recommended. `Mutalaa-Kur.cmd` is experimental; download, first launch and service setup have not been verified on a real device.
- **Linux:** Installation support has not been validated.

Use `SHA256SUMS.txt` to check downloaded file integrity.
