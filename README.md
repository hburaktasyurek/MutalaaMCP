# Mütalaa MCP

![Mütalaa MCP — Türk hukuk kaynakları, kullandığınız yapay zekâda. Ücretsiz, açık kaynak ve yerel çalışma.](docs/assets/social/mutalaa-mcp-tanitim.png)

**Kullandığınız yapay zekâ uygulamasından Türkiye’nin hukuk kaynaklarını araştırın.**
Ücretsiz, açık kaynaklı ve bilgisayarınızda çalışır.

[Kurulum rehberi](docs/client-setup/README.md) · [English](README.en.md) · [Mütalaa](https://mutalaa.tr/)

## Ne yapabilirsiniz?

- Mevzuat, yargı kararları ve Anayasa Mahkemesi kararları arayın.
- İlgili maddeyi veya belgeyi okuyun; resmî kaynak bağlantısını açın.
- “Kiracının aidat sorumluluğu nedir?” gibi gündelik sorularla araştırmaya başlayın.

## Başlayın

Bir **Mütalaa hesabı**, internet bağlantısı ve **yerel MCP bağlantısını destekleyen bir yapay zekâ uygulaması** gerekir.

1. [Kurulum rehberinden](docs/client-setup/README.md) bilgisayarınıza uygun adımları izleyin. Depoyu klonlamanız veya kod yazmanız gerekmez.
2. Uygulamanıza Mütalaa bağlantısını ekleyip hesabınızla giriş yapın.
3. Doğal hukuk sorularında araç kullanımını kolaylaştırmak için [Mütalaa becerisini](docs/mutalaa-skill.md) ekleyin.
4. Yeni bir sohbette şunu deneyin:

   > Mütalaa kullanarak 193 sayılı Gelir Vergisi Kanunu’nu bul ve resmî kaynak bağlantısını ver.

Araç çağrısında `mutalaamcp` görünüyorsa araştırma Mütalaa üzerinden yapılmıştır.
Yalnız web araması görünüyorsa [sorun giderme adımlarını](docs/client-setup/README.md#sorun-giderme) izleyin.

**Kurulum:** [Rehberdeki](docs/client-setup/README.md) terminal komutuyla belirli bir Release sürümü kurulur. macOS için imzasız yükleyici sunulmuyor; Windows CMD betiği deneysel.

## Verileriniz ve ücret

Yerel kullanım ücretsizdir; ticari kullanım kotası yoktur. Yapay zekâ uygulamanızın kendi ücretleri geçerli olabilir.
Araştırma ve önbellek bilgisayarınızda işlenir; kaynak istekleri ilgili resmî hizmetlere gider.
Mütalaa hesabı giriş ve etkinleştirme için kullanılır; hukuki sorgular ve belge metinleri etkinleştirme hizmetine gönderilmez.
Kullandığınız yapay zekâ uygulaması, sorunuz ve araç sonuçlarını kendi veri politikası kapsamında işler.

Mütalaa ürün duyuruları yanıtın sonunda ayrı bir bölümde görünebilir. [Duyuruların çalışma biçimi](docs/announcements.md).

## Geliştiriciler için

Sekiz araç, tek yerel sunucu: araştırma başlangıcı, mevzuat/karar/AYM araması, belge getirme,
madde getirme, mevzuat içinde arama ve madde ağacı. Girdiler ve yanıtlar [araç sözleşmesinde](contracts/tool-surface-v1.json) tanımlıdır.

- [Katkıda bulunma ve fork rehberi](CONTRIBUTING.md)
- [Hata bildirimi ve destek](SUPPORT.md)
- [Güvenlik açığı bildirme](SECURITY.md)

MIT lisanslıdır. [Lisans](LICENSE) · [Üçüncü taraf bildirimleri](THIRD_PARTY_NOTICES) · [Marka politikası](TRADEMARK_POLICY)

Mütalaa MCP hukuk araştırma yazılımıdır. Sonuçları birincil kaynaklardan doğrulayın; somut bir mesele için yetkin bir uzmana başvurun.
