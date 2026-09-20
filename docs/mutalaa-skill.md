# Mütalaa MCP ve becerisi / Mütalaa MCP and its skill

Beceri iş akışını öğretir; MCP güncel kaynakları, kapsamı ve araçları sağlar.
Becerinin açıklaması hukuki sorularda keşfedilmesini sağlar. İlk işi mevcut Mütalaa MCP
araçlarını keşfedip başlangıç aracını çağırmaktır; ayrıntılı araştırma kuralları ve araç
kataloğu bu aracın yanıtından alınır. Beceri içine ikinci bir katalog kopyalanmaz.

## MCP ile birlikte kurulum

Bu depodaki güncellenmiş `setup` komutu MCP bağlantısını hazırlarken aynı sürümün
becerisini de sunar; beceri için ayrıca internetten dosya indirmez. Bu özellik henüz
yayımlanmamıştır; mevcut `v0.1.0` indirmesinin birlikte beceri kurduğunu varsaymayın.
Eski paketlerde aşağıdaki beceri yükleyicisi veya elle kurulum yöntemi kullanılabilir.

| İstemci | Kurulum davranışı |
| --- | --- |
| Codex | `mutalaamcp setup --client codex --transport http` veya stdio için `mutalaamcp setup --client codex`: beceriyi `~/.agents/skills/mutalaa-turk-hukuku/SKILL.md` konumuna kurar. |
| Cursor | `mutalaamcp setup --client cursor`: aynı ortak kullanıcı beceri klasörüne kurar. |
| Claude Desktop | `mutalaamcp setup --client claude-desktop`: beceri ZIP dosyasını hazırlar ve yolunu gösterir. Claude'da **Customize > Skills** üzerinden içe aktarın ve etkinleştirin. |
| Diğer istemciler | `setup` ZIP dosyasını da hazırlar; uygulama beceri destekliyorsa kendi içe aktarma yöntemiyle ekleyin. Destek yoksa MCP'yi açıkça çağırarak kullanabilirsiniz. |

MCP ayarını komutun çıktısıyla uygulamaya ekleyin ve hesap bağlantısını tamamlayın;
`setup` MCP ayar dosyanızı değiştirmez. Beceri kurulumu veya içe aktarma tek başına
MCP bağlantısı kurmaz. İçe aktarma gereken uygulamalarda ZIP hazırlanması kurulumun
bittiği anlamına gelmez. Yeni sohbet açın; beceri görünmüyorsa uygulamayı yeniden başlatın.

Codex/Cursor kurulumu aynı içerik zaten varsa dosyayı yeniden yazmaz. Mevcut istemci
klasöründe (`~/.codex/skills/` veya `~/.cursor/skills/`) bu beceri varsa ikinci kopya
oluşturmak yerine onu günceller. Değişen eski içerik aynı klasörde `.bak` dosyasında
korunur; sembolik bağlantılar üzerine yazılmaz. Ortak klasörde ve istemciye özel
klasörde birlikte kopya bulunursa çakışan kopyaları birleştirmeniz istenir. Yalnız diğer istemcinin özel klasöründe bir kopya varsa, ortak
klasöre taşımadan yeni ortak kopya oluşturulmaz; böylece ikinci uygulamanın kurulumu
ilk uygulamada çakışma yaratmaz. Hata mesajındaki klasörlere göre mevcut beceri klasörünü
ortak konuma taşıyıp `setup` komutunu yeniden çalıştırın. Windows'ta `~` kullanıcı klasörünüzü ifade eder.

## Mevcut kurulumlar

Yukarıdaki davranış güncellenmiş `setup` komutunu gerektirir; eski sürümler beceriyi
birlikte kurmayabilir. [MCP kurulum rehberine](client-setup/README.md) göre paketi güncelleyin
ve kullandığınız istemci için `setup` komutunu yeniden çalıştırın. HTTP hizmeti çalışıyorsa
rehberdeki durdurma/yeniden kurma adımlarını izleyin. İçe aktarılan becerilerde yeni ZIP'i yükleyin.

MCP'yi zaten bağladıysanız Codex'in beceri yükleyicisine şu mesajı da verebilirsiniz:

> `$skill-installer` ile https://github.com/hburaktasyurek/MutalaaMCP/tree/main/skills/mutalaa-turk-hukuku adresindeki beceriyi kur.

Elle kurulumda depodaki `skills/mutalaa-turk-hukuku/` klasörünü Codex/Cursor için
`~/.agents/skills/` altına kopyalayın; mevcut kopyayı koruyarak güncelleyin. Bu adresin
`main` dalı yayımlanmış içeriği verir; yerel değişiklikler yayımlanana kadar orada görünmez.

İlk araştırma aracı, istemci becerinin kurulu olduğunu bildirirse öneri döndürmez.
Durum bilinmiyorsa asistandan kendi beceri listesini kontrol etmesini ister; sunucu
bilgisayarınızdaki becerileri taramaz. Eksik beceri için kurulum önerisi konuşmada bir kez
sunulur, araştırma devam eder. Tekrarlamama davranışı istemcinin yönlendirmeyi uygulamasına bağlıdır.

## Çalıştığını kontrol edin

Yeni sohbette araç adını anmadan deneyin:

- “Aidat konusunda ev sahibi ve kiracının sorumlulukları nelerdir?”
- “İşe iade konusunda Yargıtay kararlarını araştır.”
- “Bu sözleşmedeki fesih hükmünün Türk hukukundaki dayanağını kontrol et.”

Becerinin yüklendiğini, Mütalaa araçlarının keşfedildiğini, başlangıç aracının çağrıldığını
ve gerektiğinde dayanak metninin getirildiğini kontrol edin. Yalnız web araması varsa
“Mütalaa kullanarak kontrol et” deyin. Otomatik seçim modele ve uygulamaya bağlıdır;
garanti değildir. Yabancı hukuk ve yalnız yazım düzeltme gibi işler kapsam dışındadır.

Kurulum ve keşif davranışı için resmî kaynaklar:
[Codex](https://learn.chatgpt.com/docs/build-skills),
[Cursor](https://cursor.com/docs/skills),
[Claude](https://support.claude.com/en/articles/12512180-use-skills-in-claude).

## English

The skill teaches the workflow; MCP supplies current tools, scope and source material.
The description enables discovery. The skill first discovers Mütalaa MCP and calls its
entry tool, then follows the returned guidance. Tool catalogs and detailed research rules
stay in MCP rather than being copied into the skill.

The updated `mutalaamcp setup --client …` in this checkout bundles both parts of setup.
This feature has not been released yet; do not assume the existing `v0.1.0` download
bundles the skill. Older packages can use the skill installer or manual folder installation. Codex and Cursor
receive the skill in `~/.agents/skills/mutalaa-turk-hukuku/SKILL.md`. Existing client-specific
copies are updated in place, changed content is backed up, and symlinks are preserved.
If only the other client has a private copy, move it to the shared directory before
configuring the second client; setup refuses to create a conflicting shared copy.
Claude Desktop receives a ZIP to import and enable through **Customize > Skills**.
Other clients receive the same ZIP with a prompt to use their supported import method.
An exported ZIP is not reported as an installed skill.

Add the printed MCP configuration to your app and complete authentication. Start a new
conversation after installing or importing the skill. Older package versions may require
an update and another `setup` run. Re-import ZIP skills when updating them.
The entry tool offers conditional installation guidance for existing users, based on
client-reported status; it cannot inspect client skills and does not block research.
Try natural Turkish-law questions without naming Mütalaa and inspect actual tool calls.
Automatic selection is not guaranteed. See the official sources linked above.
