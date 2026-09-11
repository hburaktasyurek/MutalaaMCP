"""Self-contained consent page for the local native-client OAuth bridge."""

from __future__ import annotations


def render_page(*, client_name: str, redirect_uri: str, csrf: str, nonce: str) -> str:
    # Names/URIs are escaped by the caller; csrf and nonce are URL-safe randoms.
    return (
        _PAGE.replace("CLIENT_NAME", client_name)
        .replace("REDIRECT_URI", redirect_uri)
        .replace("CSRF_VALUE", csrf)
        .replace("NONCE_VALUE", nonce)
    )


_PAGE = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mütalaa — Uygulamanızı bağlayın</title>
<style nonce="NONCE_VALUE">
:root{color-scheme:light}body{margin:0;background:#f8f6f0;color:#252a28;font:17px/1.65 system-ui,sans-serif}
main{max-width:540px;margin:8vh auto;padding:36px}h1{font:normal 38px/1.2 Georgia,serif}
.brand{letter-spacing:.16em;text-transform:uppercase;font-size:13px;color:#65725d}
button,.link{display:inline-block;background:#344638;color:#fff;border:0;border-radius:8px;padding:14px 22px;font:inherit;cursor:pointer;text-decoration:none}
button:disabled{opacity:.6;cursor:wait}.details{font-size:13px;color:#677068;overflow-wrap:anywhere}
#status{min-height:3em}#code{font-weight:600;letter-spacing:.1em}.hidden{display:none}a{color:#344638}
.promo{margin-top:36px;padding-top:24px;border-top:1px solid #dcded5;font-size:15px;color:#677068}.promo h2{margin:0;color:#344638;font:normal 24px/1.3 Georgia,serif}.promo p{margin:10px 0}.promo a{font-weight:600;text-underline-offset:3px}
</style></head><body><main>
<p class="brand">Mütalaa</p><h1>Uygulamanızı bağlayın.</h1>
<p><strong>CLIENT_NAME</strong>, bu bilgisayardaki MutalaaMCP ile hukuk kaynaklarını araştırmak için izin istiyor.</p>
<p>Arama ve belge okuma araçları açılacak. Araştırma ve önbellek bilgisayarınızda kalacak; resmî kaynaklara internet üzerinden erişilecek.</p>
<p class="details">Bağlantı onayı şu yerel uygulama adresine dönecek:<br>REDIRECT_URI</p>
<button id="connect">İzin ver ve Mütalaa ile giriş yap</button>
<p id="status" role="status" aria-live="polite">Giriş için ayrı bir pencere açılacak. Bu sayfayı açık bırakın.</p>
<p id="code"></p><p><a id="login" class="hidden" target="_blank" rel="noopener noreferrer">Mütalaa girişini aç</a></p>
<div id="terms" class="hidden"><p>Bağlantıyı tamamlamak için Mütalaa koşullarının kabulü gerekiyor.</p>
<a id="termsLink" target="_blank" rel="noopener noreferrer">Koşulları aç</a><p><button id="retry">Koşulları kabul ettim, bağlantıyı tamamla</button></p></div>
<p class="details">İzin vermek istemiyorsanız bu sayfayı kapatabilirsiniz.</p>
<aside class="promo" aria-labelledby="promo-title">
<h2 id="promo-title">Mütalaa’yı keşfedin</h2>
<p>Mütalaa’nın hukuk araştırma araçlarını ve diğer ürünlerini inceleyin.</p>
<a href="https://mutalaa.tr/" target="_blank" rel="noopener noreferrer">Mütalaa’yı keşfet <span aria-hidden="true">↗</span><span class="details"> (yeni sekmede)</span></a>
</aside>
<script nonce="NONCE_VALUE">
const base=location.pathname, status=document.getElementById('status'), button=document.getElementById('connect');
let popup=null, opened=false, stopped=false;
async function start(){
 const response=await fetch(base+'/start',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({csrf:'CSRF_VALUE'})});
 const data=await response.json();if(!response.ok)throw new Error(data.error||'Bağlantı başlatılamadı.');
}
function fail(message){stopped=true;status.textContent=message;if(popup&&!opened)popup.close();}
async function poll(){
 if(stopped)return;
 try{
  const response=await fetch(base+'/status',{cache:'no-store'});const data=await response.json();
  if(!response.ok||data.error){fail(data.error||'Bağlantı tamamlanamadı.');return;}
  if(data.ready){stopped=true;status.textContent='Bağlantı tamamlandı. Uygulamanıza dönülüyor…';if(popup)popup.close();location.replace(base+'/complete');return;}
  if(data.terms_url){document.getElementById('terms').classList.remove('hidden');document.getElementById('termsLink').href=data.terms_url;status.textContent='Koşulların kabulü bekleniyor.';}
  else if(data.verification_uri){
   document.getElementById('code').textContent='Cihaz kodunuz: '+data.user_code;
   const link=document.getElementById('login');link.href=data.verification_uri;link.classList.remove('hidden');
   status.textContent='Açılan Mütalaa penceresinde giriş yapın ve cihaz kodunu onaylayın.';
   if(!opened){opened=true;if(popup)popup.location=data.verification_uri;}
  }
 }catch(e){fail('Yerel bağlantıya ulaşılamıyor. Uygulamadan yeniden Kimliği Doğrula seçin.');return;}
 setTimeout(poll,1500);
}
button.addEventListener('click',async()=>{
 button.disabled=true;popup=window.open('about:blank','_blank');if(popup)popup.opener=null;
 status.textContent='Giriş hazırlanıyor…';try{await start();poll();}catch(e){fail(e.message);}
});
document.getElementById('retry').addEventListener('click',async()=>{try{await start();document.getElementById('terms').classList.add('hidden');}catch(e){fail(e.message);}});
</script></main></body></html>"""
