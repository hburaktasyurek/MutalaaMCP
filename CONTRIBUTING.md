# Katkıda Bulunma / Contributing

MutalaaMCP'yi geliştirmeye yardımcı olduğunuz için teşekkürler. Bu belge Türkçe
önceliklidir; her kuralın İngilizce karşılığı yanındadır.

## Başlamadan önce / Before you start

- Küçük, açık düzeltmeler için doğrudan bir pull request açabilirsiniz. Büyük
  değişikliklerde (yeni araç, davranış değişikliği, geniş yeniden düzenleme)
  kod yazmadan önce kapsamı ve beklenen sonucu anlatan bir GitHub issue açın.
  / You may open a pull request for a small, clear fix. For a large change
  (new tool, behavior change, or broad refactor), first open a GitHub issue
  describing the scope and intended outcome.
- Bir issue veya PR'ye erişim belirteci, yenileme belirteci, API anahtarı,
  sunucu sırrı, kişisel veri, hukuki sorgu ya da belge metni eklemeyin. Günlük
  ve örnekleri ayıklayın. / Do not include access or refresh tokens, API keys,
  server secrets, personal data, legal queries, or document text in an issue
  or PR. Redact logs and examples.
- Güvenlik açığını herkese açık issue'ya yazmayın; GitHub Private Vulnerability
  Reporting kanalını kullanın. Hukuki tavsiye istenen bir katkı veya issue
  açmayın. / Do not report a vulnerability in a public issue; use GitHub
  Private Vulnerability Reporting. Do not submit a legal-advice request as a
  contribution or issue.

## Yerel geliştirme / Local development

Python 3.12 veya üzeri ile, depo kökünde geliştirme bağımlılıklarını kurun:
/ With Python 3.12 or later, install development dependencies from the
repository root:

```sh
uv sync --extra dev
```

Değişikliğinize odaklanan testi önce çalıştırın; ardından tam test kümesini
çalıştırın. / Run a test focused on your change first, then the full suite:

```sh
uv run pytest path/to/relevant_test.py
uv run pytest
```

PR hazırlamadan önce statik denetimleri de çalıştırın. / Also run the static
checks before preparing a PR:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Varsayılan testler canlı hukuk sağlayıcılarına veya başka canlı dış hizmetlere
trafik göndermemelidir. Ağ bağımlılığını sahteleyin ya da örnekleyin; canlı
entegrasyon denemelerini varsayılan test kümesinin dışında tutun. / Default
tests must not send traffic to live legal providers or other live external
services. Fake or fixture network dependencies, and keep live integration
experiments outside the default suite.

## Pull request / Pull request

- Değişikliği küçük ve tek amaçlı tutun; ilgili issue varsa bağlayın. / Keep
  the change small and single-purpose; link a related issue when one exists.
- Davranış değişikliğini, nasıl doğruladığınızı ve gerekliyse belge
  değişikliğini açıklayın. / Describe the behavior change, how you verified
  it, and any documentation update needed.
- Gizli veya hassas içerik içermeyen yeniden üretilebilir kanıt sağlayın.
  / Provide reproducible evidence without secret or sensitive content.

## Lisans / License

Katkı göndererek, katkınızın depo içindeki mevcut MIT Lisansı kapsamında
sunulabileceğini kabul edersiniz. / By submitting a contribution, you agree
that it may be made available under this repository's existing MIT License.

## Depo düzeni ve fork’lar / Repository layout and forks

| Dizin / Directory | İçerik / Contents |
| --- | --- |
| `src/` | Uygulama kodu / Application code |
| `tests/` | Tekrarlanabilir testler / Reproducible tests |
| `contracts/` | Araç ve etkinleştirme sözleşmeleri / Tool and activation contracts |
| `release/` | Dağıtım kaynakları ve istemci şablonları / Distribution resources and client templates |
| `skills/` | Kullanıcının isteğe bağlı kurduğu beceri / User-installable companion skill |
| `docs/` | Kullanım belgeleri ve proje sitesi / User documentation and project site |

Kişisel çalışma raporlarını, sohbet dökümlerini ve makineye özgü ayarları `.local/`
altında veya depo dışında tutun. `.local/`, `.codex/`, `.agents/` ve `.claude/`
Git ve paket dağıtımı dışında bırakılır. Paylaşılacak davranış kanıtlarını hassas
veri içermeyen testlere, kalıcı bilgiyi kullanım belgelerine dönüştürün.
/ Keep personal reports, chat transcripts, and machine-specific settings in
`.local/` or outside the repository. Local state directories are excluded from
Git and package builds. Turn reusable findings into sanitized tests or durable documentation.

Fork almak beceriyi kurmaz, oturum açmaz veya hizmet başlatmaz. `skills/` kullanıcıya
sunulan üründür; bu depo üzerinde çalışan bir ajana otomatik araştırma talimatı yüklemez.
/ Forking does not install a skill, sign in, or start a service. `skills/` is a
user-installable feature, not an automatically loaded instruction for repository contributors.

CI testleri fork’larda çalışabilir. Pages ve PyPI yayın işleri yalnız ana depoda
çalışır; kendi dağıtımınız için workflow koşullarını ve yayın hesaplarını bilinçli
olarak değiştirin. Paket adı, proje bağlantıları ve marka kullanımı da kendi
dağıtımınıza uygun olmalıdır; [marka politikasını](TRADEMARK_POLICY) izleyin.
/ CI can run in forks. Pages and PyPI publishing jobs are restricted to the upstream
repository. Configure your own workflow conditions, accounts, package name, links,
and branding when publishing a derivative; follow the trademark policy.

## Geliştirme hizmetleri / Development services

Varsayılan kurulum Mütalaa hizmetini kullanır. Kendi uyumlu etkinleştirme hizmetinizi
sınamak için işlem ortamında `MUTALAAMCP_AUTH_BASE_URL` ve `MUTALAAMCP_TERMS_BASE_URL`
kullanabilirsiniz. Bu adresler araştırma proxy’si değildir. Uygulama `.env` yüklemez;
kimlik sağlayıcı sırları sunucu tarafında kalır.
/ Defaults use the Mütalaa service. Set `MUTALAAMCP_AUTH_BASE_URL` and
`MUTALAAMCP_TERMS_BASE_URL` in the process environment when testing a compatible
activation service. These are not research proxies. The app does not load `.env`;
identity-provider secrets belong on the backend.

Ayrı geliştirme oturumu için `MUTALAAMCP_DATA_DIR`, `MUTALAAMCP_CACHE_DIR` ve
`MUTALAAMCP_HTTP_PORT` değerlerini ayırın; aynı kullanıcının normal kurulumu ile
veri veya port paylaşmayın. / Use separate `MUTALAAMCP_DATA_DIR`,
`MUTALAAMCP_CACHE_DIR`, and `MUTALAAMCP_HTTP_PORT` values for an isolated development
session instead of sharing the normal installation’s data or port.
