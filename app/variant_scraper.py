"""
Variant scraper for Phase 6 — its own visible Chrome via Selenium.

SEPARATE from Mr Scrape (which stays untouched).

The extraction JS below is JD's EXACT content.js from the Variant & Sibling
Chrome extension (v3), pasted verbatim. The ONLY change is the wrapper: the
extension delivered its result through chrome.runtime.onMessage; here the same
extractVariants() is called directly and its result returned to Selenium via
execute_async_script's callback. Every strategy (1A, 1A-2, 1B, 1C, 1D, 1E,
DOM 2A-2E), the bracket-matching JSON extractor, waitForVariants, buildLabel,
and the DOM-first-then-scripts merge order are unchanged.

URL is amazon.com/dp/{ASIN}?th=1 — exactly as the extension used
(variant-sibling-flow.md lines 163 / 593 / 811).
"""
import time
from typing import Dict, List, Optional

ASIN_DOMAIN = "https://www.amazon.com/dp/"
ASIN_SUFFIX = "?th=1"

_EXTRACT_JS = r"""
const done = arguments[arguments.length - 1];

function getCurrentAsin() {
  const canon = document.querySelector('link[rel="canonical"]');
  if (canon) {
    const m = canon.href.match(/\/dp\/([A-Z0-9]{10})/i);
    if (m) return m[1].toUpperCase();
  }
  const um = location.pathname.match(/\/dp\/([A-Z0-9]{10})/i);
  if (um) return um[1].toUpperCase();
  const inp = document.getElementById('ASIN') || document.querySelector('input[name="ASIN"]');
  if (inp) return inp.value.toUpperCase();
  return '';
}

function isCaptcha() {
  return !!(
    document.getElementById('captchacharacters') ||
    document.title.toLowerCase().includes('robot check') ||
    document.querySelector('form[action*="validateCaptcha"]')
  );
}

function isErrorPage() {
  const hasdog = document.getElementById('g') && document.querySelector('img[alt*="dog"]');
  const has404 = document.getElementById('404_content');
  const titleErr = document.title.toLowerCase().includes('page not found');
  return !!(hasdog || has404 || titleErr);
}

function getProductTitle() {
  const el = document.getElementById('productTitle') || document.getElementById('title');
  return el ? el.textContent.trim() : '';
}

function isValidAsin(s) {
  return /^[A-Z0-9]{10}$/.test(s);
}

function waitForVariants(maxWait) {
  return new Promise(resolve => {
    const start = Date.now();
    function check() {
      const found =
        document.querySelector('[data-defaultasin]') ||
        document.getElementById('twister') ||
        document.querySelector('[id^="variation_"]') ||
        document.querySelector('.a-button-toggle[data-defaultasin]');

      if (found) { resolve(true); return; }
      if (Date.now() - start > maxWait) { resolve(false); return; }
      setTimeout(check, 400);
    }
    check();
  });
}

function extractJSONAfterKey(text, key) {
  const idx = text.indexOf(key);
  if (idx === -1) return null;

  let start = -1;
  let openChar = '';
  for (let i = idx + key.length; i < Math.min(idx + key.length + 50, text.length); i++) {
    if (text[i] === '{') { start = i; openChar = '{'; break; }
    if (text[i] === '[') { start = i; openChar = '['; break; }
  }
  if (start === -1) return null;

  const closeChar = openChar === '{' ? '}' : ']';
  let depth = 1;
  let i = start + 1;
  let inString = false;
  let escaped = false;

  while (i < text.length && depth > 0) {
    const ch = text[i];
    if (escaped) { escaped = false; i++; continue; }
    if (ch === '\\') { escaped = true; i++; continue; }
    if (ch === '"') { inString = !inString; i++; continue; }
    if (!inString) {
      if (ch === openChar) depth++;
      else if (ch === closeChar) depth--;
    }
    i++;
  }
  if (depth !== 0) return null;

  try {
    return JSON.parse(text.substring(start, i));
  } catch {
    return null;
  }
}

function extractFromScripts() {
  const variants = {};
  const debug = { scriptCount: 0, keysFound: [] };

  const scripts = document.querySelectorAll('script:not([src])');
  debug.scriptCount = scripts.length;

  let allText = '';
  scripts.forEach(s => { allText += s.textContent + '\n'; });

  const curAsin = getCurrentAsin();

  const avv = extractJSONAfterKey(allText, '"asinVariationValues"')
    || extractJSONAfterKey(allText, 'asinVariationValues')
    || extractJSONAfterKey(allText, "'asinVariationValues'");
  const dvd = extractJSONAfterKey(allText, '"dimensionValuesDisplayData"')
    || extractJSONAfterKey(allText, 'dimensionValuesDisplayData')
    || extractJSONAfterKey(allText, "'dimensionValuesDisplayData'");

  if (avv) debug.keysFound.push('asinVariationValues');
  if (dvd) debug.keysFound.push('dimensionValuesDisplayData');

  if (dvd) {
    debug.dvdSample = {};
    for (const [k, v] of Object.entries(dvd)) {
      debug.dvdSample[k] = Array.isArray(v) ? v.slice(0, 4).join(' | ') : String(v).substring(0, 80);
    }
  }
  if (curAsin && allText.includes(curAsin)) {
    const idx = allText.indexOf(curAsin);
    debug.asinContext = allText.substring(Math.max(0, idx - 20), Math.min(allText.length, idx + 40)).replace(/\s+/g, ' ');
  }

  if (avv && dvd) {
    const dimNames = Object.keys(dvd);
    for (const [asin, indices] of Object.entries(avv)) {
      if (!isValidAsin(asin)) continue;
      const key = asin.toUpperCase();
      const labels = {};
      if (Array.isArray(indices)) {
        indices.forEach((idx, dimIdx) => {
          const dimName = dimNames[dimIdx];
          if (dimName && dvd[dimName]) {
            labels[dimName] = dvd[dimName][parseInt(idx)] || '';
          }
        });
      }
      variants[key] = { asin: key, labels };
    }
  }

  if (dvd && Object.keys(variants).length === 0) {
    const dvdKeys = Object.keys(dvd);
    const asinKeyedCount = dvdKeys.filter(k => isValidAsin(k) && typeof dvd[k] === 'string').length;
    if (asinKeyedCount >= 2) {
      debug.keysFound.push('dvd_asin_keyed');
      for (const [asin, labelStr] of Object.entries(dvd)) {
        if (!isValidAsin(asin)) continue;
        if (typeof labelStr !== 'string' || !labelStr.trim()) continue;
        const key = asin.toUpperCase();
        variants[key] = { asin: key, labels: { 'Variant': labelStr.trim() } };
      }
    }
  }

  const dav = extractJSONAfterKey(allText, 'defined_asin_variations');
  if (dav) {
    debug.keysFound.push('defined_asin_variations');
    for (const [asin, dims] of Object.entries(dav)) {
      if (!isValidAsin(asin)) continue;
      const key = asin.toUpperCase();
      if (!variants[key]) variants[key] = { asin: key, labels: {} };
      if (dims && typeof dims === 'object') {
        for (const [dim, val] of Object.entries(dims)) {
          if (typeof val === 'string') variants[key].labels[dim] = val;
        }
      }
    }
  }

  const atdim = extractJSONAfterKey(allText, '"asinToDimensionIndexMap"')
    || extractJSONAfterKey(allText, 'asinToDimensionIndexMap');
  if (atdim) {
    debug.keysFound.push('asinToDimensionIndexMap');
    if (dvd) {
      const dimNames = Object.keys(dvd);
      for (const [asin, indices] of Object.entries(atdim)) {
        if (!isValidAsin(asin)) continue;
        const key = asin.toUpperCase();
        if (!variants[key]) variants[key] = { asin: key, labels: {} };
        if (Array.isArray(indices)) {
          indices.forEach((idx, dimIdx) => {
            const dimName = dimNames[dimIdx];
            if (dimName && dvd[dimName]) {
              variants[key].labels[dimName] = dvd[dimName][parseInt(idx)] || '';
            }
          });
        }
      }
    }
  }

  if (dvd && Object.keys(variants).length === 0) {
    debug.keysFound.push('generic_asin_index_scan');
    const dimNames = Object.keys(dvd);
    const dimCount = dimNames.length;
    const asinArrRe = /["']?(B0[A-Z0-9]{8})["']?\s*:\s*\[/g;
    let match;
    while ((match = asinArrRe.exec(allText)) !== null) {
      const asin = match[1].toUpperCase();
      const arrStart = allText.indexOf('[', match.index + match[0].length - 1);
      if (arrStart === -1) continue;
      let depth = 1, j = arrStart + 1;
      while (j < allText.length && depth > 0) {
        if (allText[j] === '[') depth++;
        else if (allText[j] === ']') depth--;
        j++;
      }
      if (depth !== 0) continue;
      try {
        const arr = JSON.parse(allText.substring(arrStart, j));
        if (!Array.isArray(arr)) continue;
        if (arr.length !== dimCount) continue;
        const indices = arr.map(x => parseInt(String(x)));
        if (indices.some(isNaN)) continue;
        let valid = true;
        for (let d = 0; d < dimCount; d++) {
          if (indices[d] < 0 || indices[d] >= (dvd[dimNames[d]]?.length || 0)) {
            valid = false; break;
          }
        }
        if (!valid) continue;
        const labels = {};
        indices.forEach((idx, dimIdx) => {
          const dimName = dimNames[dimIdx];
          labels[dimName] = dvd[dimName][idx];
        });
        variants[asin] = { asin, labels };
      } catch { continue; }
    }
  }

  if (Object.keys(variants).length === 0) {
    const variationChunk = allText.match(/variation[^]*?(?=function\s|var\s|const\s|let\s|\n\n)/gi);
    if (variationChunk) {
      debug.keysFound.push('brute_force_variation');
      variationChunk.forEach(chunk => {
        const asinMatches = chunk.match(/\b(B0[A-Z0-9]{8})\b/g);
        if (asinMatches) {
          asinMatches.forEach(a => {
            const key = a.toUpperCase();
            if (!variants[key]) variants[key] = { asin: key, labels: {} };
          });
        }
      });
    }
  }

  return { variants, debug };
}

function extractFromDOM() {
  const variants = {};
  const debug = { defaultAsinCount: 0, dataAsinCount: 0, dpUrlCount: 0, dropdownCount: 0 };

  const defaultAsinEls = document.querySelectorAll('[data-defaultasin]');
  debug.defaultAsinCount = defaultAsinEls.length;

  defaultAsinEls.forEach(el => {
    const asin = (el.getAttribute('data-defaultasin') || '').trim().toUpperCase();
    if (!isValidAsin(asin)) return;

    let dimName = '';
    const section = el.closest('[id^="variation_"]');
    if (section) {
      const m = section.id.match(/variation_(.+?)(?:_name)?$/);
      if (m) dimName = m[1].replace(/_/g, ' ');
    }
    if (!dimName) {
      const labelEl = el.closest('.a-section, .a-row')
        ?.querySelector('.a-form-label, .a-color-secondary, [class*="label"]');
      if (labelEl) dimName = labelEl.textContent.replace(/[:\s]+$/g, '').trim();
    }

    let label = '';
    const btnText = el.querySelector('.a-button-text, .a-size-base, .twisterTextDiv');
    if (btnText) label = btnText.textContent.trim();
    if (!label) label = (el.getAttribute('title') || '').replace(/^Click to select\s*/i, '').trim();
    if (!label) {
      const img = el.querySelector('img');
      if (img) label = (img.alt || '').trim();
    }
    if (!label) label = (el.getAttribute('aria-label') || '').trim();
    label = label.replace(/\$[\d,.]+.*$/m, '').trim();

    if (!variants[asin]) variants[asin] = { asin, labels: {} };
    if (dimName && label) variants[asin].labels[dimName] = label;
    else if (label) variants[asin].labels['Variant'] = label;
  });

  const dataAsinEls = document.querySelectorAll('#twister [data-asin], [id^="variation_"] [data-asin]');
  debug.dataAsinCount = dataAsinEls.length;

  dataAsinEls.forEach(el => {
    const asin = (el.getAttribute('data-asin') || '').trim().toUpperCase();
    if (!isValidAsin(asin)) return;
    if (!variants[asin]) {
      let label = el.textContent.trim().substring(0, 100);
      variants[asin] = { asin, labels: { 'Variant': label } };
    }
  });

  const dpUrlEls = document.querySelectorAll('[data-dp-url]');
  debug.dpUrlCount = dpUrlEls.length;

  dpUrlEls.forEach(el => {
    const url = el.getAttribute('data-dp-url') || '';
    const m = url.match(/\/dp\/([A-Z0-9]{10})/i);
    if (!m) return;
    const asin = m[1].toUpperCase();
    if (!variants[asin]) {
      let label = (el.getAttribute('title') || '').replace(/^Click to select\s*/i, '').trim();
      if (!label) {
        const txt = el.querySelector('.a-button-text, .a-size-base');
        if (txt) label = txt.textContent.trim();
      }
      variants[asin] = { asin, labels: label ? { 'Variant': label } : {} };
    }
  });

  const selects = document.querySelectorAll('select');
  selects.forEach(sel => {
    const id = sel.id || '';
    const isVariant = id.includes('dropdown') || id.includes('variation') || id.includes('twister')
      || sel.closest('#twister, [id^="variation_"]');
    if (!isVariant) return;

    let dimName = '';
    const m = id.match(/native_dropdown_selected_(.+)/);
    if (m) dimName = m[1].replace(/_/g, ' ').trim();

    sel.querySelectorAll('option').forEach(opt => {
      const val = opt.value || '';
      let asin = '';
      if (val.includes(',')) {
        const candidate = val.split(',')[1]?.trim().toUpperCase();
        if (isValidAsin(candidate)) asin = candidate;
      } else if (isValidAsin(val.trim().toUpperCase())) {
        asin = val.trim().toUpperCase();
      }
      if (!asin) return;
      debug.dropdownCount++;

      const label = opt.textContent.trim();
      if (!variants[asin]) variants[asin] = { asin, labels: {} };
      if (dimName && label) variants[asin].labels[dimName] = label;
      else if (label) variants[asin].labels['Variant'] = label;
    });
  });

  const variantLinks = document.querySelectorAll(
    '#twister a[href*="/dp/"], [id^="variation_"] a[href*="/dp/"], .twisterSwatchWrapper a[href*="/dp/"]'
  );
  variantLinks.forEach(a => {
    const m = a.href.match(/\/dp\/([A-Z0-9]{10})/i);
    if (!m) return;
    const asin = m[1].toUpperCase();
    if (!variants[asin]) {
      const label = a.textContent.trim() || (a.querySelector('img')?.alt || '').trim();
      variants[asin] = { asin, labels: label ? { 'Variant': label } : {} };
    }
  });

  return { variants, debug };
}

function buildLabel(labels) {
  const entries = Object.entries(labels).filter(([k, v]) => v && v.trim() && v !== '-');
  if (entries.length === 0) return '';
  if (entries.length === 1) return entries[0][1].trim();
  return entries.map(([, v]) => v.trim()).join(' - ');
}

async function extractVariants() {
  const asin = getCurrentAsin();

  if (isCaptcha()) {
    return { asin, captcha: true, variants: [], title: '', error: null, debug: {} };
  }
  if (isErrorPage()) {
    return { asin, captcha: false, variants: [], title: '', error: 'Page not found', debug: {} };
  }

  const found = await waitForVariants(6000);

  const scriptResult = extractFromScripts();
  const domResult = extractFromDOM();

  const merged = {};
  for (const source of [domResult.variants, scriptResult.variants]) {
    for (const [key, data] of Object.entries(source)) {
      if (!merged[key]) merged[key] = { asin: key, labels: {} };
      Object.assign(merged[key].labels, data.labels);
    }
  }

  const variants = [];
  for (const [key, data] of Object.entries(merged)) {
    variants.push({
      asin: key,
      label: buildLabel(data.labels),
      isCurrent: key === asin
    });
  }

  const title = getProductTitle();

  return {
    asin,
    captcha: false,
    variants,
    title,
    error: null,
    debug: {
      waitFound: found,
      scripts: scriptResult.debug,
      dom: domResult.debug,
      totalMerged: variants.length
    }
  };
}

extractVariants().then(r => done(r)).catch(e => done({
  asin: getCurrentAsin(), captcha: false, variants: [], title: '',
  error: 'Extraction error: ' + e.message, debug: { exception: e.message }
}));
"""


# ── JD's exact LABEL_V6 label reader from the extension's fetchLabelForAsin().
#    Verbatim: reads "Color: Teal" style lines from the visible product area,
#    checks for a numeric size, pulls numeric info from the title if missing,
#    and combines "numeric - variant". Returns a single label string.
_LABEL_JS = r"""
const parts = [];
const seen = new Set();
const _v = 'LABEL_V6';

function isGarbage(t) {
  if (!t || t.length > 50) return true;
  if (/currently\s*unavailable/i.test(t)) return true;
  if (/temporarily\s*out\s*of\s*stock/i.test(t)) return true;
  if (/out\s*of\s*stock/i.test(t)) return true;
  if (/back\s*in\s*stock/i.test(t)) return true;
  if (/we\s*don'?t\s*know/i.test(t)) return true;
  if (/options?\s*from/i.test(t)) return true;
  if (/see\s*(available|\d+|more)/i.test(t)) return true;
  if (/featured\s*offers?/i.test(t)) return true;
  if (/no\s*featured/i.test(t)) return true;
  if (/^select\s/i.test(t)) return true;
  if (/add\s*to\s*(cart|list|registry)/i.test(t)) return true;
  if (/subscribe\s*&?\s*save/i.test(t)) return true;
  if (/customers\s*usually/i.test(t)) return true;
  if (/click\s*to\s*select/i.test(t)) return true;
  if (t.includes('$')) return true;
  if (/\.\s*[A-Z]/.test(t) && t.split(' ').length > 6) return true;
  return false;
}

function addPart(text) {
  let t = (text || '').replace(/\s+/g, ' ').trim();
  const di = t.indexOf('$');
  if (di > 0) t = t.substring(0, di).trim();
  t = t.replace(/\s*\($/, '').trim();
  if (isGarbage(t)) return;
  const key = t.toLowerCase();
  if (seen.has(key)) return;
  seen.add(key);
  parts.push(t);
}

const dimNames = ['Size', 'Style', 'Color', 'Flavor', 'Scent', 'Pattern',
                 'Format', 'Count', 'Pack', 'Material', 'Configuration',
                 'Strength', 'Bundle', 'Set', 'Item Package Quantity'];
const dimRe = new RegExp(
  '(?:' + dimNames.join('|') + ')\\s*(?:Name|name)?\\s*[:：]\\s*(.+)',
  'i'
);

const areas = [
  document.getElementById('centerCol'),
  document.getElementById('ppd'),
  document.getElementById('dp-container'),
  document.querySelector('#twister')
].filter(Boolean);

const searchArea = areas.length > 0 ? areas[0] : document.body;
const visibleText = searchArea.innerText || '';
const lines = visibleText.split('\n');

for (const line of lines) {
  const trimmed = line.trim();
  if (!trimmed || trimmed.length > 80) continue;
  const m = trimmed.match(dimRe);
  if (m) {
    let val = m[1].trim();
    const dIdx = val.indexOf('$');
    if (dIdx > 0) val = val.substring(0, dIdx).trim();
    val = val.replace(/\d+\s*options?\b.*$/i, '').trim();
    val = val.replace(/\bfrom\b.*$/i, '').trim();
    addPart(val);
  }
}

document.querySelectorAll('.selection').forEach(el => addPart(el.textContent));

const realVariant = parts.join(' - ');

const numericUnitRe = /\d+\s*(?:\.\d+)?\s*(?:fl\.?\s*oz|fluid\s*ounce|ounce|oz|ml|l\b|gram|g\b|count|ct\b|pack|piece|pcs|tablet|capsule|gumm|mg|iu)/i;
const hasNumericSize = numericUnitRe.test(realVariant) || /pack\s*of\s*\d+/i.test(realVariant);

let titleNumeric = '';
if (!hasNumericSize) {
  const titleEl = document.getElementById('productTitle') || document.getElementById('title');
  const title = titleEl ? titleEl.textContent.trim() : '';
  if (title) {
    const chunkRe = /\d+\s*(?:\.\d+)?\s*(?:fl\.?\s*oz|fluid\s*ounce|ounces?|oz|ml|l\b|grams?|g\b|counts?|ct\b|packs?|pieces?|pcs|tablets?|capsules?|gummies|mg|iu)(?:\s*(?:x|×|\(pack\s*of\)?)\s*\d+)?/gi;
    const matches = title.match(chunkRe);
    if (matches && matches.length > 0) {
      const cleaned = [];
      const cSeen = new Set();
      matches.forEach(m => {
        const c = m.trim().replace(/\s+/g, ' ');
        const k = c.toLowerCase();
        if (!cSeen.has(k)) { cSeen.add(k); cleaned.push(c); }
      });
      const packM = title.match(/(?:\b\d+\s*pack\b|\bx\s*\d+\b|\b\d+\s*×\s*\d+\b)/i);
      if (packM) {
        const pk = packM[0].trim().replace(/\s+/g, ' ');
        if (!cSeen.has(pk.toLowerCase())) cleaned.push(pk);
      }
      titleNumeric = cleaned.join(', ');
    }
  }
}

if (titleNumeric && realVariant) return titleNumeric + ' - ' + realVariant;
if (titleNumeric && !realVariant) return titleNumeric;
return realVariant;
"""


class VariantScrapeError(Exception):
    pass

class VariantScraper:
    def __init__(self, visible: bool = True, page_wait: float = 4.0) -> None:
        self.visible = visible
        self.page_wait = page_wait
        self._driver = None

    def _ensure_driver(self):
        if self._driver is not None:
            return self._driver
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError as exc:
            raise VariantScrapeError(
                "selenium is not installed. Run: pip install selenium"
            ) from exc

        opts = Options()
        if not self.visible:
            opts.add_argument("--headless=new")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1200,900")

        # Persistent profile: the delivery pincode / address you set once is
        # remembered here, so you don't re-enter it on every page or every run.
        # Lives next to the tool at ./chrome_variant_profile.
        import os as _os
        from pathlib import Path as _Path
        profile_dir = _os.environ.get("VARIANT_CHROME_PROFILE")
        if not profile_dir:
            profile_dir = str(_Path(__file__).resolve().parent.parent
                              / "chrome_variant_profile")
        try:
            _os.makedirs(profile_dir, exist_ok=True)
            opts.add_argument(f"--user-data-dir={profile_dir}")
        except Exception:
            pass  # fall back to a temp profile if the dir can't be made

        try:
            driver = webdriver.Chrome(options=opts)
        except Exception as exc:
            raise VariantScrapeError(
                f"Could not start Chrome for variant scraping: {exc}. "
                f"Make sure Chrome + chromedriver are installed (Mr Scrape uses the same)."
            ) from exc

        try:
            driver.execute_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
        except Exception:
            pass
        try:
            driver.set_script_timeout(30)
        except Exception:
            pass
        self._driver = driver
        return driver

    def scrape(self, asin: str) -> Dict:
        driver = self._ensure_driver()
        url = ASIN_DOMAIN + asin + ASIN_SUFFIX
        try:
            driver.get(url)
        except Exception as exc:
            raise VariantScrapeError(f"navigation failed for {asin}: {exc}") from exc

        time.sleep(self.page_wait)

        try:
            for b in driver.find_elements("css selector",
                                          "button, input[type=submit], a"):
                t = (b.text or b.get_attribute("value") or "").strip().lower()
                if "continue shopping" in t or t == "dismiss":
                    b.click()
                    time.sleep(1.5)
                    break
        except Exception:
            pass

        try:
            data = driver.execute_async_script(_EXTRACT_JS)
        except Exception as exc:
            raise VariantScrapeError(f"extraction failed for {asin}: {exc}") from exc

        if not isinstance(data, dict):
            raise VariantScrapeError(f"unexpected extraction result for {asin}")

        data.setdefault("asin", asin.upper())
        data.setdefault("title", "")
        data.setdefault("captcha", False)
        vs = []
        for v in data.get("variants") or []:
            va = str(v.get("asin", "")).upper()
            if len(va) == 10:
                vs.append({"asin": va,
                           "label": str(v.get("label") or "").strip(),
                           "isCurrent": bool(v.get("isCurrent"))})
        data["variants"] = vs
        return data

    def fetch_label(self, asin: str) -> str:
        """
        JD's exact fetchLabelForAsin: reload amazon.com/dp/<asin>?th=1 (up to 3
        tries), run LABEL_V6, return that ASIN's own label
        (e.g. "Black Camouflage - Flip 5"). Empty string if none found.
        """
        driver = self._ensure_driver()
        for _attempt in range(3):
            try:
                driver.get(ASIN_DOMAIN + asin + ASIN_SUFFIX)
            except Exception:
                continue
            time.sleep(2.5)
            try:
                label = driver.execute_script("return (function(){" + _LABEL_JS + "})();")
            except Exception:
                label = None
            if isinstance(label, str) and label.strip():
                return label.strip()
        return ""

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
