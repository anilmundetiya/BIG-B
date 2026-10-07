"""
SEO engine for Phase 8 — Mr. Scout's exact pipeline, driven by Selenium.

This ports Scout's Chrome-extension logic into the ten-asin tool:
  - extractors.js  -> _JS_AMAZON, _JS_SERP, _JS_LENS_* (verbatim function bodies)
  - lens-auto.js   -> _JS_LENS_UPLOAD, _JS_LENS_AI, _JS_LENS_EXACT (verbatim)
  - gemini-auto.js -> _JS_GEMINI_INJECT, _JS_GEMINI_SEND, _JS_GEMINI_CHECK (verbatim)
  - generator.js   -> imported as Python (build_prompt, parse_gemini_response)
  - background.js  -> runResearch orchestration mirrored in ScoutSEO.research()
                      URL scoring (SOURCE_POINTS, SKIP_DOMAINS, TLD, non-Latin),
                      page selection (multi-source, top-5 high, top-2 low, cap 12)

Uses the SAME visible Chrome + persistent profile as the variant scraper, so the
operator's Gemini / Google login persists. Mr Scrape untouched.
"""
import re
import time
import json
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from . import seo_prompt

AMAZON_IN = "https://www.amazon.in/dp/"
GOOGLE_HOME = "https://www.google.co.in/"
LENS_HOME = "https://lens.google.com/"
LENS_UPLOAD_BY_URL = "https://lens.google.com/uploadbyurl?url="
GEMINI_APP = "https://gemini.google.com/app"

# ── Scout constants (background.js) ──
SOURCE_POINTS = {"title": 2, "barcode": 3, "asin": 1, "lens": 3}
SKIP_DOMAINS = {
    'youtube.com','google.com','google.co.in','facebook.com','instagram.com',
    'twitter.com','x.com','pinterest.com','reddit.com','quora.com','wikipedia.org',
    'shazam.com','spotify.com','soundcloud.com','pubmed.ncbi.nlm.nih.gov',
    'ncbi.nlm.nih.gov','overdrive.com','goodreads.com','linkedin.com','tiktok.com',
    'alibaba.com','aliexpress.com','aliexpress.us','1688.com','taobao.com',
    'tmall.com','jd.com','pinduoduo.com','dhgate.com','made-in-china.com',
    'chinabrands.com','globalsources.com','banggood.com','gearbest.com',
    'lightinthebox.com','miniinthebox.com','tomtop.com','chinavasion.com',
    'dx.com','dealextreme.com','tradeindia.com','exportersindia.com',
    'zaful.com','shein.com','temu.com','wish.com','joom.com',
    'focalprice.com','newfrog.com','osell.com','ecvv.com','hktdc.com',
    'diytrade.com','tradekey.com','kompass.com','dir.indiamart.com','thezlab.es',
    'bidplus.gem.gov.in','hetwil.nl','omnewyork.com','icra.in','nextproject.fr',
    'watarutomita.net','galaxy-i.com','linearcollider.org','m.forestoy.com',
    'sohobaby.com','mrsmeyers.com','wildberries.ru','ozon.ru','market.yandex.ru',
    'yandex.ru','anglingcentrewestbay.co.uk','rakuten.co.jp','yahoo.co.jp','kakaku.com',
    'yelp.com','trustpilot.com','glassdoor.com','bbb.org','sitejabber.com',
    'manta.com','yellowpages.com','superpages.com','angieslist.com',
}
SKIP_TLD_SUFFIXES = [
    '.cn','.com.cn','.net.cn','.org.cn','.gov.cn','.hk','.tw','.gov.in','.shop',
    '.jp','.co.jp','.kr','.co.kr','.ru','.su','.by','.ua','.bg','.rs','.mk',
    '.sa','.ae','.eg','.iq','.ir','.kw','.qa','.bh','.jo','.lb','.ly','.dz','.ma','.tn',
    '.vn','.co.vn','.th','.co.th','.id','.co.id','.my','.com.my','.pk',
]
DOWNLOAD_EXTENSIONS = {
    'pdf','zip','rar','7z','tar','gz','tgz','bz2','doc','docx','xls','xlsx','csv',
    'ppt','pptx','rtf','odt','ods','odp','epub','mobi','exe','msi','dmg','pkg','deb',
    'rpm','apk','bin','iso','jpg','jpeg','png','gif','webp','svg','bmp','tiff','ico',
    'avif','mp3','mp4','mov','avi','mkv','wav','flac','webm','m4a','wmv','json','xml',
    'txt','dwg','psd','ai',
}
_NONLATIN_RE = re.compile(
    "[\u0400-\u04FF\u0600-\u06FF\u0900-\u097F\u3000-\u9FFF\uAC00-\uD7AF\u0E00-\u0E7F\u0590-\u05FF]"
)
SCRAPE_CONCURRENCY = 4  # Scout default (we scrape sequentially here for simplicity)


def _has_non_latin(text: str) -> bool:
    if not text:
        return False
    nl = len(_NONLATIN_RE.findall(text))
    stripped = re.sub(r"\s", "", text)
    return nl > 0 and stripped and (nl / len(stripped)) > 0.2


def _is_download_url(raw: str) -> bool:
    try:
        u = urllib.parse.urlparse(raw)
        if u.scheme not in ("http", "https"):
            return True
        path = u.path.lower()
        dot = path.rfind(".")
        if dot == -1:
            return False
        return path[dot + 1:] in DOWNLOAD_EXTENSIONS
    except Exception:
        return True


def clean_query(title: str) -> str:
    # background.js cleanQuery
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", title or "")).strip()


# ══════════════════════════════════════════════════════════════════
# Scout's extractor / gemini JS — function BODIES, verbatim, each
# wrapped to `return (function(){ <body> })();` for execute_script.
# ══════════════════════════════════════════════════════════════════

_JS_AMAZON = r"""
return (function(){
  try {
    const title = document.querySelector('#productTitle')?.textContent?.trim() || '';
    if (!title) return { found: false };
    const byline = document.querySelector('#bylineInfo');
    let brand = '';
    if (byline) brand = byline.textContent.replace(/^(Visit the |Brand: )/i,'').replace(/ Store$/i,'').trim();
    const bullets = [];
    document.querySelectorAll('#feature-bullets li span.a-list-item').forEach(el => {
      const t = el.textContent?.trim();
      if (t && t.length > 5 && !t.includes('Make sure this fits')) bullets.push(t);
    });
    const description = document.querySelector('#productDescription p')?.textContent?.trim() ||
                        document.querySelector('#productDescription')?.textContent?.trim() || '';
    let ingredients='', howToUse='';
    document.querySelectorAll('h3, h4, h5, .a-text-bold').forEach(h => {
      const t=h.textContent?.trim()?.toLowerCase()||''; const n=h.nextElementSibling?.textContent?.trim()||'';
      if (t.includes('ingredient') && n) ingredients=n;
      if ((t.includes('direction')||t.includes('how to use')) && n) howToUse=n;
    });
    const importantInfo = document.querySelector('#important-information');
    if (!ingredients && importantInfo) {
      importantInfo.querySelectorAll('.content').forEach(s => {
        const t=s.textContent?.trim(); if (t && t.length>50 && !ingredients) ingredients=t;
      });
    }
    const rating = document.querySelector('#acrPopover span.a-icon-alt')?.textContent?.trim() || '';
    const reviewCount = document.querySelector('#acrCustomerReviewText')?.textContent?.trim() || '';
    return { found:true, title, brand, bullets, description, ingredients, howToUse, rating, reviewCount };
  } catch(e) { return { found:false, error:e.message }; }
})();
"""

_JS_SERP = r"""
return (function(){
  try {
    const bodyText = document.body.innerText || '';
    const isCaptcha = bodyText.includes('unusual traffic') || bodyText.includes('not a robot') ||
      bodyText.includes('automated queries') || bodyText.includes('CAPTCHA') ||
      document.querySelector('#captcha-form') !== null ||
      document.querySelector('iframe[src*="recaptcha"]') !== null;
    if (isCaptcha) return { error:'RATE_LIMIT', fullPageText:'', resultTitles:[], paaQuestions:[], relatedSearches:[], resultUrls:[] };
    const mainContent = document.querySelector('#main')||document.querySelector('#search')||document.querySelector('#rso')||document.body;
    const fullText = mainContent?.innerText || bodyText;
    const resultTitles=[];
    document.querySelectorAll('h3').forEach(el=>{ const t=el.textContent?.trim(); if(t&&t.length>5&&t.length<200)resultTitles.push(t); });
    const resultUrls=[];
    document.querySelectorAll('a[href^="http"]').forEach(link=>{
      const href=link.href||'';
      if(href.includes('google.')||href.includes('gstatic.')||href.includes('googleapis.'))return;
      if(href.includes('accounts.')||href.includes('support.')||href.includes('policies.'))return;
      try{ const domain=new URL(href).hostname.replace('www.','');
        const title=link.querySelector('h3')?.textContent?.trim()||link.textContent?.trim()?.substring(0,100)||'';
        if(title&&title.length>3&&!resultUrls.find(r=>r.domain===domain))resultUrls.push({url:href,domain,title});
      }catch(e){}
    });
    const paaQuestions=[];
    fullText.split('\n').forEach(line=>{ const t=line.trim();
      if(t.endsWith('?')&&t.length>15&&t.length<200){
        if(t.includes('unusual traffic')||t.includes('not a robot')||t.includes('Why did this happen'))return;
        if(!paaQuestions.includes(t))paaQuestions.push(t);
      }
    });
    const relatedSearches=[];
    const SKIP=new Set(['Sign in','Settings','Privacy','Terms','About','Advertising','How Search works','AI Mode','Shopping','Images','Videos','Short videos','Forums','More','Tools','News','Books','Maps','All','Search','Next','Previous','Feedback','Help','Any country','Country: India','Country: United States','Any time','Past hour','Past 24 hours','Past week','Past month','Past year','Custom range','All results','Verbatim','Learn more','Send feedback','See more','See all','Cached','Similar','Translate this page']);
    document.querySelectorAll('a').forEach(link=>{ const href=link.href||'';
      if(href.includes('google.')&&href.includes('/search')&&href.includes('q=')){
        const text=link.textContent?.trim();
        if(text&&text.length>3&&text.length<80&&!SKIP.has(text)&&!text.match(/^\d+$/)&&!relatedSearches.includes(text))relatedSearches.push(text);
      }
    });
    return { fullPageText:fullText.substring(0,4000), resultTitles:resultTitles.slice(0,10),
      paaQuestions:paaQuestions.slice(0,8), relatedSearches:relatedSearches.slice(0,10), resultUrls:resultUrls.slice(0,15) };
  } catch(e){ return { error:e.message, fullPageText:'', resultTitles:[], paaQuestions:[], relatedSearches:[], resultUrls:[] }; }
})();
"""

_JS_GOOGLE_TYPE = r"""
var q = arguments[0];
return (function(){
  const input = document.querySelector('textarea[name="q"]') || document.querySelector('input[name="q"]');
  if(!input) return { success:false, error:'No search input found' };
  input.focus(); input.value=''; input.value=q;
  input.dispatchEvent(new Event('input',{bubbles:true}));
  input.dispatchEvent(new Event('change',{bubbles:true}));
  const form=input.closest('form');
  if(form){ form.submit(); return { success:true }; }
  input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,bubbles:true}));
  return { success:true, method:'keydown' };
})();
"""

_JS_ANY_PAGE = r"""
return (function(){
  try {
    const bodyText = document.body?.innerText || '';
    const isCloudflare = bodyText.includes('Checking your browser')||bodyText.includes('Performing security verification')||
      bodyText.includes('Verifying you are human')||bodyText.includes('Cloudflare')||bodyText.includes('Ray ID')||
      bodyText.includes('Please wait while we verify')||bodyText.includes('Enable JavaScript and cookies')||
      bodyText.includes('you are not a bot')||document.title?.includes('Just a moment')||
      document.title?.includes('Attention Required')||document.title?.includes('Security check')||
      document.querySelector('iframe[src*="challenges.cloudflare"]')!==null;
    if(isCloudflare) return { error:'RATE_LIMIT', title:document.title||'' };
    let structured=null;
    document.querySelectorAll('script[type="application/ld+json"]').forEach(s=>{
      try{ const d=JSON.parse(s.textContent);
        if(d['@type']==='Product')structured=d;
        if(Array.isArray(d)){const p=d.find(x=>x['@type']==='Product');if(p)structured=p;}
        if(d['@graph']){const p=d['@graph'].find(x=>x['@type']==='Product');if(p)structured=p;}
      }catch(e){}
    });
    const title=structured?.name||document.querySelector('h1')?.textContent?.trim()||document.title?.split('|')[0]?.trim()||'';
    const brand=structured?.brand?.name||'';
    const rawDesc=structured?.description;
    const description=(Array.isArray(rawDesc)?rawDesc.join(' '):(typeof rawDesc==='string'?rawDesc:''))||
      document.querySelector('[class*="description"], [class*="product-desc"], [id*="description"]')?.textContent?.trim()?.substring(0,1500)||'';
    let ingredients='', howToUse='';
    document.querySelectorAll('h2, h3, h4').forEach(h=>{ const t=h.textContent?.trim()?.toLowerCase()||'';
      const n=h.nextElementSibling?.textContent?.trim()?.substring(0,1000)||'';
      if(t.includes('ingredient')&&n&&!ingredients)ingredients=n;
      if((t.includes('how to use')||t.includes('direction'))&&n&&!howToUse)howToUse=n;
    });
    const metaDesc=document.querySelector('meta[name="description"]')?.getAttribute('content')||'';
    const rawText=document.body?.innerText?.substring(0,2000)||'';
    return { title:title||document.title||'', brand, description:description||rawText.substring(0,500),
      ingredients, howToUse, metaDesc, rawText:(!title&&!description)?rawText:'' };
  } catch(e){ const f=document.body?.innerText?.substring(0,1500)||''; return { title:document.title||'', description:f, error:e.message }; }
})();
"""

_JS_LENS_UPLOAD = r"""
var base64Data = arguments[0];
return (function(){
  try {
    const parts=base64Data.split(','); const mime=parts[0].match(/:(.*?);/)[1];
    const byteString=atob(parts[1]); const ab=new ArrayBuffer(byteString.length); const ia=new Uint8Array(ab);
    for(let i=0;i<byteString.length;i++)ia[i]=byteString.charCodeAt(i);
    const blob=new Blob([ab],{type:mime}); const file=new File([blob],'product.jpg',{type:'image/jpeg'});
    let fileInput=document.querySelector('input[type="file"]');
    if(!fileInput){ document.querySelectorAll('button,[role="button"],[aria-label*="upload"],[aria-label*="image"],[aria-label*="camera"],[aria-label*="photo"]').forEach(btn=>{ const n=btn.closest('div')?.querySelector('input[type="file"]'); if(n)fileInput=n; }); }
    if(!fileInput){ const all=document.querySelectorAll('input[type="file"], input[accept*="image"]'); if(all.length>0)fileInput=all[0]; }
    if(!fileInput){ const sels=['[aria-label*="Search by image"]','[aria-label*="search by image"]','[aria-label*="Upload"]','[aria-label*="upload"]','[data-action="camera"]','button svg']; for(const s of sels){ const el=document.querySelector(s); if(el){ (el.closest('button')||el.closest('[role="button"]')||el).click(); break; } } return { success:false, needsRetry:true }; }
    const dt=new DataTransfer(); dt.items.add(file); fileInput.files=dt.files;
    fileInput.dispatchEvent(new Event('change',{bubbles:true})); fileInput.dispatchEvent(new Event('input',{bubbles:true}));
    return { success:true };
  } catch(e){ return { success:false, error:e.message }; }
})();
"""

_JS_LENS_AI = r"""
return (function(){
  try {
    let overviewText='';
    const allElements=document.querySelectorAll('span, div, h2, h3');
    for(const el of allElements){ const text=el.textContent?.trim();
      if(text==='AI Overview'){ let container=el.parentElement;
        for(let i=0;i<5;i++){ if(!container)break; const ct=container.innerText?.trim()||'';
          if(ct.length>150&&ct.includes('AI Overview')){ overviewText=ct.replace(/^AI Overview\s*/i,'').replace(/मराठी/g,'').replace(/Show more\s*$/i,'').trim(); break; }
          container=container.parentElement;
        } if(overviewText.length>50)break;
      }
    }
    if(!overviewText||overviewText.length<50){ const lists=document.querySelectorAll('ul');
      for(const ul of lists){ const items=ul.querySelectorAll('li');
        if(items.length>=2){ const lt=Array.from(items).map(li=>li.innerText?.trim()).filter(t=>t.length>20);
          if(lt.length>=2){ const hasLabels=lt.some(t=>/^(Product Type|Ingredients|Formulation|Key Features|Dietary|Suggested Use|Version|Usage|Description):/i.test(t));
            if(hasLabels){ const prev=ul.previousElementSibling; const intro=prev?.innerText?.trim()||''; overviewText=(intro.length>30?intro+'\n':'')+lt.join('\n'); break; }
          }
        }
      }
    }
    if(overviewText&&overviewText.length>50)return { success:true, text:overviewText.substring(0,2000) };
    return { success:false, text:'' };
  } catch(e){ return { success:false, text:'', error:e.message }; }
})();
"""

_JS_LENS_EXACT_CLICK = r"""
return (function(){
  try {
    let found=false;
    document.querySelectorAll('a, button, [role="tab"], [role="link"]').forEach(el=>{
      if(found)return; const t=el.textContent?.trim();
      if(t==='Exact matches'||t==='Exact match'){ el.click(); found=true; }
    });
    if(!found){ document.querySelectorAll('a[href]').forEach(link=>{ if(found)return; if(link.textContent?.toLowerCase().includes('exact')){ link.click(); found=true; } }); }
    return { success:found };
  } catch(e){ return { success:false, error:e.message }; }
})();
"""

_JS_LENS_SCRAPE = r"""
return (function(){
  try {
    const products=[];
    document.querySelectorAll('a[href]').forEach(link=>{ const href=link.href||'';
      if(!href.startsWith('http'))return;
      if(href.includes('google.com')||href.includes('google.co')||href.includes('gstatic.'))return;
      if(href.includes('accounts.')||href.includes('support.')||href.includes('policies.'))return;
      const title=link.textContent?.trim()?.substring(0,150)||''; if(!title||title.length<3)return;
      try{ const domain=new URL(href).hostname.replace('www.',''); if(!products.find(p=>p.domain===domain))products.push({url:href,domain,title}); }catch(e){}
    });
    return { success:products.length>0, sites:products.slice(0,15) };
  } catch(e){ return { success:false, sites:[], error:e.message }; }
})();
"""

# gemini-auto.js — verbatim bodies
_JS_GEMINI_INJECT = r"""
var promptText = arguments[0];
return (function(){
  try {
    const inputSelectors=['.ql-editor[contenteditable="true"]','[contenteditable="true"][role="textbox"]','rich-textarea [contenteditable="true"]','[contenteditable="true"][aria-label*="prompt"]','[contenteditable="true"][aria-label*="message"]','[contenteditable="true"][data-placeholder]','textarea[aria-label*="prompt"]','textarea[aria-label*="message"]','[contenteditable="true"]'];
    let input=null;
    for(const sel of inputSelectors){ const el=document.querySelector(sel);
      if(el){ const rect=el.getBoundingClientRect(); if(rect.width>200||sel.includes('textarea')){ input=el; break; } }
    }
    if(!input) return { success:false, error:'Could not find Gemini input field' };
    input.focus();
    if(input.tagName==='TEXTAREA'){ input.value=promptText; input.dispatchEvent(new Event('input',{bubbles:true})); }
    else {
      input.textContent=''; input.textContent=promptText;
      input.dispatchEvent(new Event('input',{bubbles:true})); input.dispatchEvent(new Event('change',{bubbles:true}));
      try{ input.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:promptText})); }catch(e){}
    }
    return { success:true, inputFound:true };
  } catch(e){ return { success:false, error:e.message }; }
})();
"""

_JS_GEMINI_SEND = r"""
return (function(){
  try {
    const sendSelectors=['button[aria-label*="Send"]','button[aria-label*="send"]','button[data-at="send"]','button[aria-label*="Submit"]','.send-button','button:has(svg[viewBox])'];
    let sendBtn=null;
    for(const sel of sendSelectors){ try{ const el=document.querySelector(sel); if(el&&!el.disabled){ sendBtn=el; break; } }catch(e){} }
    if(!sendBtn){ const buttons=document.querySelectorAll('button:not([disabled])');
      const inputArea=document.querySelector('[contenteditable="true"]')||document.querySelector('textarea');
      if(inputArea&&buttons.length>0){ let closest=null,cd=Infinity; const ir=inputArea.getBoundingClientRect();
        buttons.forEach(btn=>{ const br=btn.getBoundingClientRect(); const d=Math.abs(br.bottom-ir.bottom)+Math.abs(br.right-ir.right); if(d<cd&&br.width<100){cd=d;closest=btn;} });
        if(closest)sendBtn=closest;
      }
    }
    if(!sendBtn) return { success:false, error:'Could not find send button' };
    sendBtn.click(); return { success:true };
  } catch(e){ return { success:false, error:e.message }; }
})();
"""

_JS_GEMINI_CHECK = r"""
return (function(){
  try {
    const responseSelectors=['[data-message-author-role="model"]','.model-response-text','.response-container','.message-content','.markdown-content','.conversation-container > div:last-child','[data-content-type="response"]','.response-content','message-content','.model-response'];
    let responseEl=null;
    for(const sel of responseSelectors){ const els=document.querySelectorAll(sel); if(els.length>0){ responseEl=els[els.length-1]; break; } }
    if(!responseEl){ const allDivs=document.querySelectorAll('div'); let longest=null,ll=0;
      allDivs.forEach(div=>{ const text=div.innerText?.trim()||''; if(text.length>ll&&text.length>200&&div.children.length>0){ if(!div.querySelector('[contenteditable]')&&!div.closest('[contenteditable]')){ longest=div; ll=text.length; } } });
      responseEl=longest;
    }
    if(!responseEl) return { done:false, text:'' };
    const text=responseEl.innerText?.trim()||'';
    const actionSel=['button[aria-label*="Good"]','button[aria-label*="good"]','button[aria-label*="Bad"]','button[aria-label*="bad"]','button[aria-label*="Copy"]','button[aria-label*="copy"]','button[aria-label*="Modify"]','button[aria-label*="Retry"]','button[aria-label*="Share"]','button[aria-label*="More"]','button[aria-label*="Like"]','button[aria-label*="Dislike"]','button[aria-label*="Rewrite"]','button[aria-label*="Export"]','[data-tooltip*="Copy"]','[data-tooltip*="Good"]','[data-tooltip*="Like"]'];
    let hasActionButtons=false;
    for(const sel of actionSel){ try{ const btns=document.querySelectorAll(sel);
      for(const btn of btns){ const rect=btn.getBoundingClientRect(); if(rect.bottom>window.innerHeight*0.3){ hasActionButtons=true; break; } }
    }catch(e){} if(hasActionButtons)break; }
    const stopSel=['[aria-label*="Stop"]','[aria-label*="stop"]','button[aria-label*="Stop generating"]','button[aria-label*="Cancel"]'];
    let isStreaming=false;
    for(const sel of stopSel){ try{ if(document.querySelector(sel)){ isStreaming=true; break; } }catch(e){} }
    const inputField=document.querySelector('[contenteditable="true"]')||document.querySelector('textarea');
    const inputDisabled=inputField&&(inputField.getAttribute('aria-disabled')==='true'||inputField.closest('[aria-disabled="true"]')!==null);
    const done=(hasActionButtons||(text.length>100&&!isStreaming&&!inputDisabled))&&!isStreaming&&text.length>50;
    return { done:done, text:text, length:text.length, streaming:isStreaming, hasActionButtons:hasActionButtons };
  } catch(e){ return { done:false, text:'', error:e.message }; }
})();
"""


class ScoutSEOError(Exception):
    pass


class ScoutSEO:
    """
    One instance per Phase 8 run. Reuses a single visible Chrome (persistent
    profile) across all products so Google/Gemini login sticks. Mirrors Scout's
    runResearch() per product. logger(msg, level) streams to the job log.
    """

    def __init__(self, visible: bool = True, page_wait: float = 4.0, logger=None):
        self.visible = visible
        self.page_wait = page_wait
        self.log = logger or (lambda m, l="info": None)
        self._driver = None

    def _ensure_driver(self):
        if self._driver is not None:
            return self._driver
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError as exc:
            raise ScoutSEOError("selenium not installed. Run: pip install selenium") from exc

        import os as _os
        from pathlib import Path as _Path
        opts = Options()
        if not self.visible:
            opts.add_argument("--headless=new")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1280,900")
        # SAME persistent profile as the variant scraper: Gemini/Google login sticks
        profile = _os.environ.get("VARIANT_CHROME_PROFILE")
        if not profile:
            profile = str(_Path(__file__).resolve().parent.parent / "chrome_variant_profile")
        try:
            _os.makedirs(profile, exist_ok=True)
            opts.add_argument(f"--user-data-dir={profile}")
        except Exception:
            pass
        try:
            driver = webdriver.Chrome(options=opts)
        except Exception as exc:
            raise ScoutSEOError(f"Could not start Chrome for SEO: {exc}") from exc
        try:
            driver.execute_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            driver.set_page_load_timeout(40)
            driver.set_script_timeout(30)
        except Exception:
            pass
        self._driver = driver
        return driver

    # ── low-level helpers ──
    def _open(self, url: str, wait: float = None):
        d = self._ensure_driver()
        d.get(url)
        time.sleep(self.page_wait if wait is None else wait)

    def _js(self, script: str, *args):
        d = self._ensure_driver()
        try:
            return d.execute_script(script, *args)
        except Exception:
            return None

    # ── autocomplete (fetch, no tab) — Scout getAutocomplete ──
    def _autocomplete(self, query: str) -> List[str]:
        try:
            url = ("https://www.google.com/complete/search?q="
                   + urllib.parse.quote(query) + "&gl=in&hl=en&client=chrome")
            with urllib.request.urlopen(url, timeout=8) as r:
                d = json.loads(r.read().decode("utf-8", "ignore"))
            if isinstance(d, list) and len(d) > 1 and isinstance(d[1], list):
                return [s for s in d[1] if isinstance(s, str) and len(s) > 3][:15]
        except Exception:
            pass
        return []

    # ── URL scoring (background.js addUrlsWithSource) ──
    def _add_urls(self, url_scores: dict, serp: dict, source: str, brand: str):
        if not serp or not serp.get("resultUrls"):
            return
        pts = SOURCE_POINTS.get(source, 1)
        b = (brand or "").lower()
        for u in serp["resultUrls"]:
            dom = (u.get("domain") or "")
            if not dom:
                continue
            if _is_download_url(u.get("url", "")):
                continue
            dom = re.sub(r"^www\.", "", dom)
            if dom in SKIP_DOMAINS or dom.endswith(".gov") or dom.endswith(".edu"):
                continue
            if any(dom.endswith(t) for t in SKIP_TLD_SUFFIXES):
                continue
            if _has_non_latin(dom) or _has_non_latin(u.get("title", "")):
                continue
            if source == "asin" and b and u.get("title"):
                if b not in u["title"].lower():
                    continue
            if dom in url_scores:
                url_scores[dom]["score"] += pts
                if source not in url_scores[dom]["sources"]:
                    url_scores[dom]["sources"].append(source)
            else:
                url_scores[dom] = {"url": u["url"], "domain": dom,
                                   "title": u.get("title", ""), "score": pts,
                                   "sources": [source]}

    # ── Google search via typed UI (background.js searchGoogleUI) ──
    def _search_google(self, query: str) -> Optional[dict]:
        d = self._ensure_driver()
        typed = self._js(_JS_GOOGLE_TYPE, query)
        if not typed or not typed.get("success"):
            return None
        time.sleep(3)
        for _ in range(8):
            chk = self._js(
                "return {hasResults: (document.querySelector('#search')!==null||document.querySelector('#rso')!==null),"
                "readyState: document.readyState, contentLength:(document.body?document.body.innerText.length:0)};")
            if chk and chk.get("hasResults") and chk.get("readyState") == "complete" and (chk.get("contentLength") or 0) > 500:
                break
            time.sleep(2)
        return self._js(_JS_SERP)

    # ══════════════════════════════════════════════════════════════
    # research(product) — mirrors background.js runResearch()
    # product = {sku, asin, title, barcode, images: [url,...], category}
    # returns parsed SEO dict (from seo_prompt.parse_gemini_response) or None
    # ══════════════════════════════════════════════════════════════
    def research(self, product: dict) -> Optional[dict]:
        asin = product.get("asin") or ""
        title = product.get("title") or ""
        barcode = product.get("barcode") or ""
        images = product.get("images") or []
        brand_guess = (product.get("category") or title.split(" ")[0] if title else "") or ""

        state = {
            "productData": product,
            "steps": {"amazonIn": {}, "googleLens": {}},
            "serpData": {},
            "keywords": [],
            "scrapedData": {},
            "scrapedScores": {},
        }
        url_scores: dict = {}

        # == 1. Amazon.in ==
        if asin:
            self.log(f"    SEO stage 1/8 Amazon.in {asin}")
            self._open(AMAZON_IN + asin, wait=self.page_wait)
            data = self._js(_JS_AMAZON)
            state["steps"]["amazonIn"] = {"data": data if isinstance(data, dict) else None}

        # == open google home ==
        if barcode or title or asin:
            self._open(GOOGLE_HOME, wait=2.5)

        # == 2. Barcode ==
        if barcode:
            self.log("    SEO stage 2/8 barcode search")
            b = self._search_google(barcode)
            if b and b.get("error") != "RATE_LIMIT":
                self._add_urls(url_scores, b, "barcode", brand_guess)
            time.sleep(4)
            self._open(GOOGLE_HOME, wait=2.0)  # fresh home for next query

        # == 3. Google title+ASIN search + autocomplete ==
        search_title = title or (state["steps"]["amazonIn"].get("data") or {}).get("title") or ""
        if search_title:
            self.log("    SEO stage 3/8 Google search + keywords")
            cq = clean_query(search_title)
            ac1 = self._autocomplete(cq)
            brand = (re.match(r"^(\S+)", search_title) or [None, ""])[1] if search_title else ""
            ac2 = self._autocomplete(brand + " " + " ".join(cq.split(" ")[1:3]) + " india") if brand else []
            kw = list(ac1)
            for k in ac2:
                if k not in kw:
                    kw.append(k)
            state["keywords"] = kw

            td = self._search_google(search_title)
            if td and td.get("error") != "RATE_LIMIT":
                state["serpData"]["titleSearch"] = td
                self._add_urls(url_scores, td, "title", brand_guess)
                if asin:
                    time.sleep(4)
                    self._open(GOOGLE_HOME, wait=2.0)
                    ad = self._search_google(asin)
                    if ad and ad.get("error") != "RATE_LIMIT":
                        state["serpData"]["asinSearch"] = ad
                        self._add_urls(url_scores, ad, "asin", brand_guess)

        # == 4. Google Lens ==
        if images:
            self.log("    SEO stage 4/8 Google Lens")
            self.log(f"      [trace] lens: image url = {images[0][:120]}")
            try:
                reached = False
                # --- Method 1: uploadbyurl (no upload, Google fetches the image) ---
                if images[0].lower().startswith("http"):
                    lens_url = (LENS_UPLOAD_BY_URL
                                + urllib.parse.quote(images[0], safe=""))
                    try:
                        self._open(lens_url, wait=4.0)
                    except Exception:
                        pass  # page-load timeout is fine — URL already changed
                    for _ in range(8):
                        cur = self._ensure_driver().current_url or ""
                        if "/search" in cur or "vsrid=" in cur:
                            reached = True
                            break
                        time.sleep(2)
                    if not reached:
                        # one more check — URL may have changed during the timeout
                        cur = self._ensure_driver().current_url or ""
                        if "/search" in cur or "vsrid=" in cur:
                            reached = True
                    if reached:
                        time.sleep(5)  # let Lens finish rendering AI overview
                    cur = self._ensure_driver().current_url or ""
                    self.log(f"      [trace] lens: uploadbyurl -> reached={reached} | {cur[:120]}")

                # --- Method 2 fallback: native Selenium file upload (real browser event) ---
                if not reached:
                    self.log("      [trace] lens: uploadbyurl failed, trying native file upload")
                    tmp_path = self._image_to_tempfile(images[0])
                    self.log(f"      [trace] lens: tempfile -> {tmp_path or 'NONE'}")
                    if tmp_path:
                        try:
                            self._open(LENS_HOME, wait=3.0)
                            from selenium.webdriver.common.by import By
                            d = self._ensure_driver()
                            finp = None
                            for el in d.find_elements(By.CSS_SELECTOR,
                                                      'input[type="file"]'):
                                finp = el
                                break
                            if finp is not None:
                                try:
                                    d.execute_script(
                                        "arguments[0].removeAttribute('hidden');"
                                        "arguments[0].style.display='block';"
                                        "arguments[0].style.visibility='visible';"
                                        "arguments[0].style.opacity=1;", finp)
                                except Exception:
                                    pass
                                finp.send_keys(tmp_path)   # REAL upload, isTrusted event
                                for _ in range(10):
                                    cur = self._ensure_driver().current_url or ""
                                    if "/search" in cur or "vsrid=" in cur:
                                        reached = True
                                        break
                                    time.sleep(2)
                                cur = self._ensure_driver().current_url or ""
                                self.log(f"      [trace] lens: native upload -> reached={reached} | {cur[:120]}")
                            else:
                                self.log("      [trace] lens: no file input found on page", "warn")
                        finally:
                            try:
                                import os as _os
                                _os.unlink(tmp_path)
                            except Exception:
                                pass

                # --- Harvest results (same as before) ---
                if reached:
                    time.sleep(2)
                    ai = self._js(_JS_LENS_AI)
                    self._js(_JS_LENS_EXACT_CLICK)
                    time.sleep(3)
                    lr = self._js(_JS_LENS_SCRAPE)
                    ai_text = ai.get("text") if (ai and ai.get("success")) else None
                    self.log(f"      [trace] lens: aiOverview -> {('ok ' + str(len(ai_text)) + ' chars') if ai_text else 'EMPTY'}")
                    self.log(f"      [trace] lens: sites scraped -> {len((lr or {}).get('sites') or [])}")
                    state["steps"]["googleLens"] = {"data": {"aiOverview": ai_text,
                                                             "sites": (lr or {}).get("sites", [])}}
                    if lr and lr.get("success") and lr.get("sites"):
                        self._add_urls(url_scores, {"resultUrls": lr["sites"]}, "lens", brand_guess)
                else:
                    self.log("      [trace] lens: both methods failed - skipping Lens", "warn")
            except Exception as exc:
                self.log(f"      Lens failed: {exc}", "warn")
        else:
            self.log("      [trace] lens: SKIPPED (no product images)")

        # == 6. Select + scrape top pages (Scout selection rule) ==
        pages = sorted(url_scores.values(), key=lambda p: p["score"], reverse=True)
        self.log(f"      [trace] pool: {len(pages)} url(s) collected across all sources (barcode+title+asin+lens)")
        amazon_data = state["steps"]["amazonIn"].get("data") or {}
        filtered = []
        for p in pages:
            if not p.get("domain"):
                continue
            if len(p.get("sources", [])) >= 2:
                filtered.append(p); continue
            if p["domain"] in ("amazon.in", "www.amazon.in") and amazon_data.get("found") \
                    and amazon_data.get("bullets") and amazon_data.get("description"):
                continue
            if "google." in p["domain"] or "youtube." in p["domain"]:
                continue
            filtered.append(p)
        self.log(f"      [trace] pool: {len(filtered)} after filter (drops google/youtube/amazon-if-covered)")
        multi = [p for p in filtered if len(p.get("sources", [])) >= 2]
        high = [p for p in filtered if p["score"] >= 3 and len(p.get("sources", [])) < 2]
        low = [p for p in filtered if p["score"] < 3 and len(p.get("sources", [])) < 2]
        to_scrape = (multi + high[:5] + low[:2])[:12]
        seen, deduped = set(), []
        for p in to_scrape:
            if p["domain"] in seen:
                continue
            seen.add(p["domain"]); deduped.append(p)
        self.log(f"      [trace] pool: buckets = multi:{len(multi)} high:{len(high)} low:{len(low)} | after domain-dedup = {len(deduped)}")
        if deduped:
            self.log(f"      [trace] pool: final domains = {[p['domain'] for p in deduped]}")

        if deduped:
            self.log(f"    SEO stage 6/8 scraping {len(deduped)} competitor page(s)")
            for p in deduped:
                try:
                    self._open(p["url"], wait=self.page_wait)
                    dat = self._js(_JS_ANY_PAGE)
                    if isinstance(dat, dict) and dat.get("error") != "RATE_LIMIT":
                        state["scrapedData"][p["domain"]] = dat
                        state["scrapedScores"][p["domain"]] = p["score"]
                except Exception:
                    continue

        # == 7. Build prompt (generator.js buildPrompt) ==
        self.log("    SEO stage 7/8 building prompt")
        prompt = seo_prompt.build_prompt(product, state)

        # == 8. Gemini ==
        self.log("    SEO stage 8/8 Gemini generating")
        raw = self._drive_gemini(prompt)
        if not raw:
            self.log("      Gemini returned nothing", "warn")
            return None
        parsed = seo_prompt.parse_gemini_response(raw)
        if not parsed.get("success"):
            self.log(f"      Gemini parse failed: {parsed.get('error')}", "warn")
            return None
        return parsed["data"]

    # ── Gemini driver (background.js autoGenerate, 3 attempts) ──
    def _drive_gemini(self, prompt: str) -> Optional[str]:
        GEMINI_MAX_RETRIES = 3
        for attempt in range(1, GEMINI_MAX_RETRIES + 1):
            try:
                self._open(GEMINI_APP, wait=6.0 if attempt == 1 else 3.0)
                inj = None
                for _ in range(5):
                    inj = self._js(_JS_GEMINI_INJECT, prompt)
                    if inj and inj.get("success"):
                        break
                    time.sleep(3)
                if not inj or not inj.get("success"):
                    continue
                time.sleep(1)
                snd = self._js(_JS_GEMINI_SEND)
                if not snd or not snd.get("success"):
                    continue

                text = ""
                start = time.time()
                last_len = 0
                stable = 0
                while time.time() - start < 180:
                    time.sleep(2)
                    chk = self._js(_JS_GEMINI_CHECK)
                    if not chk:
                        if time.time() - start > 30:
                            break
                        continue
                    if chk.get("done") and len(chk.get("text", "")) > 50:
                        text = chk["text"]; break
                    if len(chk.get("text", "")) > 500 and not chk.get("streaming"):
                        if len(chk["text"]) == last_len:
                            stable += 1
                            if stable >= 5:
                                text = chk["text"]; break
                        else:
                            stable = 0
                    else:
                        stable = 0
                    last_len = len(chk.get("text", ""))

                if text and len(text) >= 50:
                    return text
            except Exception:
                pass
            time.sleep(3)
        return None

    def _image_to_base64(self, image_url: str) -> Optional[str]:
        try:
            req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                ctype = r.headers.get("Content-Type", "image/jpeg")
            import base64 as _b64
            return f"data:{ctype};base64," + _b64.b64encode(raw).decode("ascii")
        except Exception:
            return None

    def _image_to_tempfile(self, image_url: str) -> Optional[str]:
        """Download the product image to a real temp file so Selenium can
        send_keys() it into the file input (a genuine, trusted browser upload)."""
        try:
            req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
            import tempfile as _tf
            fd, path = _tf.mkstemp(suffix=".jpg", prefix="lens_")
            import os as _os
            with _os.fdopen(fd, "wb") as fh:
                fh.write(raw)
            return path
        except Exception:
            return None

    def close(self):
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
