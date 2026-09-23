## 0.1.1

- Aynı kullanıcı için birden fazla stdio bağlantısı açılabilir. Claude Desktop sohbet ve Cowork için ayrı süreçler açtığında ikinci bağlantının `already_running` hatasıyla kapanmasına neden olan kilit çakışması giderildi.
- İstemciler kimlik doğrulama ve önbellek için aynı yerel sunucuyu paylaşır. Bir istemci kapandığında diğerleri çalışmaya devam eder; güncelleme ve bakım kilitleri korunur.
- MCP bağlantısında Mütalaa'nın kendi sürümü gösterilir; FastMCP kütüphanesinin sürümü gösterilmez.
- `setup`, Codex/Cursor için Mütalaa becerisini kurar; Claude Desktop ve diğer istemciler için içe aktarılabilir ZIP hazırlar.
- Güncelleme manifesti yayın dosyalarına eklendi. `0.1.0` kullanıcıları [güncelleme rehberindeki](https://github.com/hburaktasyurek/MutalaaMCP/blob/v0.1.1/docs/client-setup/README.md#güncelleme-ve-kaldırma) yeniden kurma ve istemci ayarı adımlarını izlemelidir.

Gerçek Claude Desktop/Cowork uygulamasıyla Windows doğrulaması henüz yapılmamıştır.

### Changes in English

- Multiple stdio clients can share one local runtime. This fixes the `already_running` collision when Claude Desktop starts separate chat and Cowork processes.
- Closing one client leaves the others connected. Authentication, cache, maintenance locks and update rollback remain coordinated.
- MCP reports the Mütalaa package version instead of the FastMCP library version.
- `setup` installs the companion skill for Codex/Cursor and exports a skill ZIP for Claude Desktop and other clients.
- The release includes the update manifest. Users upgrading from `0.1.0` should follow the [reinstallation and client setup steps](https://github.com/hburaktasyurek/MutalaaMCP/blob/v0.1.1/docs/client-setup/README.en.md#update-and-uninstall).

Windows validation with the actual Claude Desktop/Cowork app is still pending.

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
