# Mütalaa becerisini ekleyin / Add the Mütalaa skill

Beceri, Türk hukuku sorularında Mütalaa araçlarını bulmayı ve dayanak metinlerini okumayı tarif eder.
MCP’nin ayrıca kurulu ve hesabınızla bağlı olması gerekir. Beceri tek başına sunucuyu kurmaz veya giriş yapmaz.

## Kolay kurulum

Beceri yükleyicisi bulunan Codex’te şu mesajı gönderin:

> `$skill-installer` ile https://github.com/hburaktasyurek/MutalaaMCP/tree/main/skills/mutalaa-turk-hukuku adresindeki beceriyi kur.

Kurulumdan sonra yeni sohbet açın. Beceri görünmüyorsa uygulamayı yeniden başlatın.
Beceri içe aktarmayı destekleyen başka bir uygulamada [beceri klasörünü](../skills/mutalaa-turk-hukuku) o uygulamanın kurulum yöntemiyle ekleyin.

Elle Codex kurulumu için klasörü `~/.agents/skills/mutalaa-turk-hukuku/` altına kopyalayın;
`SKILL.md` doğrudan bu klasörde olmalıdır. Windows’ta aynı konum kullanıcı klasörünüzün altındaki `.agents/skills/` dizinidir.
Önceki bir kurulum varsa ikinci kopya eklemek yerine mevcut kopyayı güncelleyin.

## Çalıştığını kontrol edin

Yeni sohbette “Aidat konusunda ev sahibi ve kiracının sorumlulukları nelerdir?” gibi,
araç adını anmayan bir soru sorun. Çağrı kaydında Mütalaa aracı ve ardından kaynak metninin getirildiğini kontrol edin.
Yalnız web araması görünüyorsa “Mütalaa kullanarak kontrol et” deyin ve becerinin yüklü olduğunu doğrulayın.

Otomatik seçim modele ve uygulamaya bağlıdır; garanti değildir. Kullanıcının açık kaynak tercihi korunur.
Yabancı hukuk ve hukuk araştırması gerektirmeyen görevler becerinin kapsamı dışındadır.

## English

The skill guides discovery of Mütalaa tools and reading source text before answering Turkish-law questions.
Install and authenticate the MCP server separately. In Codex, ask:

> Use `$skill-installer` to install https://github.com/hburaktasyurek/MutalaaMCP/tree/main/skills/mutalaa-turk-hukuku.

Start a new conversation; restart the app if the skill does not appear. For manual Codex installation,
copy the folder to `~/.agents/skills/mutalaa-turk-hukuku/`, with `SKILL.md` directly inside it.
On Windows this is under your user folder. Update an existing copy instead of installing a duplicate.
Other apps need their own supported skill import method.

Try a natural Turkish-law question without mentioning the tool. Inspect actual Mütalaa calls and source retrieval;
implicit selection is not guaranteed. Explicit source preferences and non-Turkish-law scope are respected.

[Official skill installation and discovery documentation](https://learn.chatgpt.com/docs/build-skills).
