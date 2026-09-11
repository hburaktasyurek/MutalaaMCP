# Kurulum ve ilk kullanım

[English](README.en.md) · [Ana sayfa](../../README.md)

Bu rehber macOS ve Windows içindir. Bir Mütalaa hesabı ve yerel MCP destekleyen yapay zekâ uygulaması gerekir.
macOS’ta uygulama içi giriş denenmiştir; Windows hizmeti için gerçek cihaz doğrulaması henüz tamamlanmamıştır.
Uygulamanızda MCP özelliğinin açık olması gerekir; erişim uygulama sürümüne ve hesabınıza bağlıdır.

## 1. Terminalden kurulum

macOS için imzasız çift tıklamalı yükleyici sunulmuyor. Windows CMD betiği deneysel; gerçek indirme ve ilk açılış akışı henüz doğrulanmadı. Ana kurulum yöntemi aşağıdadır.

Kurulumu **uv** adlı paket yöneticisi yapar; Python ortamını sizin yerinize hazırlar.
Depoyu indirip klasör yönetmeniz gerekmez. uv yoksa [resmî kurulum sayfasından](https://docs.astral.sh/uv/getting-started/installation/) işletim sisteminize uygun adımı uygulayın, sonra terminali kapatıp yeniden açın.

macOS’ta **Terminal**, Windows’ta **PowerShell** açın. Aşağıdaki komutu yapıştırıp Enter’a basın:

```sh
uv tool install --python 3.12 "https://github.com/hburaktasyurek/MutalaaMCP/releases/download/v0.1.0/mutalaamcp-0.1.0-py3-none-any.whl"
```

Bu komut yayımlanmış `v0.1.0` Release paketini kurar; PyPI yayını gerektirmez.
İndirme ve kurulum bitince:

```sh
uv tool update-shell
```

Terminali kapatıp yeniden açın ve kurulumu kontrol edin:

```sh
mutalaamcp --help
```

Komut listesi görünüyorsa kurulum tamamdır. Bu yöntem için Git veya önceden kurulmuş Python gerekmez; uv gerektiğinde Python indirir.
[uv araç kurulumunun açıklaması](https://docs.astral.sh/uv/guides/tools/).

## 2. Uygulamanıza bağlayın

### Yerel URL ve uygulama içi giriş

Yerel HTTP MCP ve OAuth destekleyen uygulamalar için:

```sh
mutalaamcp setup --client codex --transport http
```

Komut arka planda çalışan kullanıcı hizmetini kurar. Yönetici parolası gerektiren sistem hizmeti oluşturmaz.
Uygulamanızın MCP ekleme ekranında şu değerleri kullanın:

| Alan | Değer |
| --- | --- |
| Ad | `mutalaamcp` |
| URL | `http://127.0.0.1:18769/mcp` |
| Taşıyıcı token / başlıklar | Boş bırakın |

Eski Mütalaa kaydınız varsa `command`/`args` yerine bu URL’yi kullanın; ikinci bir kayıt eklemeyin.
Codex’in TOML yapılandırmasını kullananlar için aynı ayar:

```toml
[mcp_servers.mutalaamcp]
url = "http://127.0.0.1:18769/mcp"
```

Bağlantıyı etkinleştirin, gerekirse uygulamayı yeniden açın ve **Kimliği Doğrula** seçin.
Tarayıcıdaki bağlantı sayfasında izin verin; açılan Mütalaa penceresinde giriş ve cihaz onayını tamamlayın.
İlk bağlantı sayfasını açık bırakın. İşlem tamamlanınca uygulamaya dönün.

Bu yolda ayrıca `auth login` çalıştırılmaz. `127.0.0.1` kendi bilgisayarınızdır; bu adresi uzaktan bağlantı isteyen bir bulut hizmetine ekleyemezsiniz.
MCP ayar ekranında tek tek araçların listelenmemesi tek başına bağlantı hatası değildir.

### Diğer uygulamalar: komutla bağlantı (stdio)

Uygulamanız yerel URL yerine komut çalıştırıyorsa bu alternatifi kullanın. HTTP hizmetini daha önce kurduysanız önce `mutalaamcp service stop` çalıştırın.

```sh
mutalaamcp auth login
```

Tarayıcıdaki giriş ve cihaz onayını tamamlayın. Sonra uygulamanızı seçin:

| Uygulama | Komut |
| --- | --- |
| Claude Desktop | `mutalaamcp setup --client claude-desktop` |
| Cursor | `mutalaamcp setup --client cursor` |
| Codex | `mutalaamcp setup --client codex` |
| Google Antigravity | `mutalaamcp setup --client google-antigravity` |
| Zcode | `mutalaamcp setup --client zcode` |
| OpenCode Desktop | `mutalaamcp setup --client opencode-desktop` |
| Witsy | `mutalaamcp setup --client witsy` |
| Hermes Agent | `mutalaamcp setup --client hermes-agent` |
| Cherry Studio | `mutalaamcp setup --client cherry-studio` |

Çıktıyı uygulamanızın MCP yapılandırmasına ekleyin. Witsy ve Cherry Studio için komut yolu ve `serve` değerini ayrı alanlara girin.
`setup` uygulamanızın ayar dosyasını değiştirmez; çıktıyı panoya kopyalamayı dener.
Bu liste yapılandırma şablonlarını gösterir; her uygulama sürümünün doğrulandığı anlamına gelmez.
Aynı kullanıcı için tek sunucu çalıştırın; HTTP ve stdio’yu birlikte başlatmayın.

## 3. İlk araştırmayı deneyin

Yeni sohbette:

> Mütalaa kullanarak 193 sayılı Gelir Vergisi Kanunu’nu bul ve resmî kaynak bağlantısını ver.

Çağrı kaydında `mutalaamcp` göründüğünü ve bir kaynak döndüğünü kontrol edin.
Sonra [Mütalaa becerisini ekleyin](../mutalaa-skill.md) ve yeni sohbette “Aidat konusunda ev sahibi ve kiracının sorumlulukları nelerdir?” gibi bir soru deneyin.
Beceri araç seçimini destekler; modelin her zaman seçmesini garanti etmez.

## Sorun giderme

| Sorun | Yapılacak işlem |
| --- | --- |
| `uv` bulunamadı | uv kurulumunu tamamlayıp yeni terminal açın. |
| `mutalaamcp` bulunamadı | `uv tool update-shell` çalıştırıp yeni terminal açın. |
| Kimliği Doğrula görünmüyor | URL kaydını kontrol edin. `mutalaamcp service status` ile hizmet durumunu görün; gerekirse `mutalaamcp service start` çalıştırıp uygulamayı yeniden açın. |
| Giriş kodu süresi doldu / `invalid_grant` | HTTP bağlantısında uygulamadan yeni kimlik doğrulama başlatın; stdio’da `mutalaamcp auth login` çalıştırın. Eski kodu kullanmayın. |
| E-posta veya koşul onayı isteniyor | Mütalaa hesabındaki adımı tamamlayıp yeniden giriş yapın. |
| Yalnız web araması yapıyor | Yeni sohbette açıkça “Mütalaa kullan” deyin. Beceri kurulumunu kontrol edin. MCP çağrısı olmadan bağlantının kullanıldığı kabul edilmez. |
| `already_running` | Diğer istemci/sunucuyu kapatın; HTTP hizmeti açıksa ayrıca stdio başlatmayın. |
| Kaynak geçici olarak kullanılamıyor / 429 / 503 | Biraz bekleyin, varsa belirtilen bekleme süresine uyun. |

Sorun sürerse [destek rehberindeki](../../SUPPORT.md) bilgilerle bildirin; token, kişisel veri veya dava metni paylaşmayın.

## Güncelleme ve kaldırma

Yeni sürüme geçerken aşağıdaki paket adresini yeni Release’in wheel adresiyle değiştirin. Aynı adres aynı sürümü yeniden kurar. Beceriyi ayrıca güncelleyin.


**Bu rehberdeki uv kurulumu için**, HTTP kullanıyorsanız hizmeti durdurun; stdio kullanıyorsanız uygulamayı kapatın:

```sh
mutalaamcp service stop
uv tool install --force --refresh --python 3.12 "https://github.com/hburaktasyurek/MutalaaMCP/releases/download/v0.1.0/mutalaamcp-0.1.0-py3-none-any.whl"
mutalaamcp setup --client codex --transport http
```

Stdio kullanıyorsanız ilk ve son hizmet adımı yerine kendi `setup --client ...` komutunuzu çalıştırın.
Uygulamayı yeniden açın. Beceriyi ayrı kurduysanız onu da güncelleyin.

Kaldırmak için önce uygulamadan MCP kaydını silin, ardından HTTP hizmetini kaldırın:

```sh
mutalaamcp service stop
mutalaamcp auth logout
uv tool uninstall mutalaamcp
```

HTTP hizmetinin bir sonraki oturum açılışında başlamaması için ayrıca kayıt dosyasını/görevini kaldırın:

macOS:

```sh
rm ~/Library/LaunchAgents/tr.mutalaa.mcp.plist
```

Windows PowerShell:

```powershell
schtasks /Delete /TN tr.mutalaa.mcp /F
```

Stdio kurulumunda hizmet adımlarını atlayın. Kaldırma araştırma önbelleğini otomatik silmez; silmek istiyorsanız paket kaldırılmadan önce `mutalaamcp cache clear` çalıştırın.

## İsteğe bağlı OCR

Taranmış PDF’ler için OCR ayrı kurulur. Önce hizmeti durdurun veya stdio istemcisini kapatın:

```sh
uv tool install --force --python 3.12 "mutalaamcp[ocr] @ https://github.com/hburaktasyurek/MutalaaMCP/releases/download/v0.1.0/mutalaamcp-0.1.0-py3-none-any.whl"
mutalaamcp ocr install
mutalaamcp ocr status
```

`ready` görünmelidir. Ardından HTTP kullanıyorsanız `mutalaamcp setup --client codex --transport http` ile hizmeti yeniden kurun; stdio kullanıyorsanız uygulamayı açın.
OCR modelleri macOS arm64 ve Windows x64 için sunulur. İlk indirme internet gerektirir.
