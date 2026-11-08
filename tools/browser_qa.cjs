/* Optional real-browser QA. No npm dependency is required by the application. */
"use strict";
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");
const os = require("os");
const { spawn } = require("child_process");
const { createHash } = require("crypto");
const CODE_ROOT = path.resolve(__dirname, "..");
const PUBLIC_FIXTURE_TOKEN = "public-browser-fixture-token-not-a-real-credential";
const PUBLIC_OTHER_FIXTURE_TOKEN = "public-other-browser-fixture-not-a-real-credential";
let activeBrowser = null;
let server = null;
let temporary = null;

function options() {
  const args = process.argv.slice(2);
  let output = path.join(CODE_ROOT, "output", "browser-qa-" + new Date().toISOString().replace(/[:.]/g, ""));
  let replace = false;
  for (let index = 0; index < args.length; index++) {
    if (args[index] === "--output" && args[index + 1]) output = path.resolve(args[++index]);
    else if (args[index] === "--replace") replace = true;
    else throw new Error("Use --output <fresh-directory> and optional --replace.");
  }
  if (fs.existsSync(output) && !replace) throw new Error("Output already exists; choose a fresh directory or explicitly use --replace.");
  fs.mkdirSync(output, { recursive: true });
  return output;
}

function pythonSources(directory) {
  const names = [];
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) names.push(...pythonSources(file));
    else if (entry.isFile() && entry.name.endsWith(".py")) names.push(path.relative(CODE_ROOT, file).split(path.sep).join("/"));
  }
  return names;
}

function sourceHashes() {
  const names = [...pythonSources(path.join(CODE_ROOT, "src", "festive_offers")),
    "data/dataset.json", "web/index.html", "web/style.css", "web/app.js",
    "tools/browser_qa_server.py", "tools/browser_qa.cjs"].sort();
  return Object.fromEntries(names.map(name => [name, createHash("sha256").update(fs.readFileSync(path.join(CODE_ROOT, name))).digest("hex")]));
}

async function startFixture() {
  temporary = fs.mkdtempSync(path.join(os.tmpdir(), "festive-browser-qa-"));
  const ready = path.join(temporary, "ready.json");
  const env = { ...process.env, PYTHONPATH: [path.join(CODE_ROOT, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter) };
  server = spawn(process.env.PYTHON || "python3", [path.join(__dirname, "browser_qa_server.py"), "--ready-file", ready],
    { cwd: CODE_ROOT, env, stdio: ["ignore", "ignore", "pipe"] });
  let failure = "";
  server.stderr.on("data", chunk => { failure = (failure + chunk.toString()).slice(-4000); });
  server.on("error", error => { failure = error.message; });
  const deadline = Date.now() + 15000;
  while (!fs.existsSync(ready)) {
    if (server.exitCode !== null || failure) throw new Error("The isolated browser fixture could not start: " + failure);
    if (Date.now() > deadline) throw new Error("Timed out starting the isolated browser fixture.");
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  return JSON.parse(fs.readFileSync(ready, "utf8")).url;
}

async function cleanup() {
  if (activeBrowser) await activeBrowser.close();
  if (server && server.exitCode === null) {
    server.kill("SIGTERM");
    await new Promise(resolve => {
      const timeout = setTimeout(() => { server.kill("SIGKILL"); resolve(); }, 3000);
      server.once("exit", () => { clearTimeout(timeout); resolve(); });
    });
  }
  if (temporary) fs.rmSync(temporary, { recursive: true, force: true });
}

(async () => {
 const destination = options();
 const sourceAtStart = sourceHashes();
 const base = await startFixture();
 const out = path.join(temporary, "artifacts");
 fs.mkdirSync(out);
 const chromePath = process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
 const browser = await chromium.launch({headless:true, executablePath:chromePath});
 activeBrowser=browser;
 const page = await browser.newPage({viewport:{width:1280,height:960},deviceScaleFactor:1});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const checks=[];
 function assert(v,label){if(!v)throw new Error(label);checks.push(label);}
 await page.goto(base,{waitUntil:'networkidle'});
 assert(await page.title()==='E-Shop · Festive offer reference checkout','page title and static assets loaded');
 await page.fill('#checkout-id','reference-checkout');
 await page.click('#quote-button');
 await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('Enter the customer credential'));
 checks.push('missing credential has a clear accessible error');
 await page.fill('#customer-token',PUBLIC_FIXTURE_TOKEN);
 await page.click('#quote-button');
 await page.waitForSelector('.offer-card');
 assert(await page.locator('.offer-card').count()===2,'trusted quote shows two eligible offers');
 assert(await page.locator('.offer-card').first().textContent().then(t=>t.includes('1,200.00')&&t.includes('960.00')),'full contract benefit and lower expected value are visible together');
 assert(await page.inputValue('#customer-token')==='','credential field is cleared after use');
 assert(await page.evaluate(()=>localStorage.length===0&&sessionStorage.length===0),'credential and retry identity are not persisted in browser storage');
 assert(await page.locator('#quote-expiry').textContent().then(t=>t.includes('UTC')),'server quote expiry is displayed without browser-clock blocking');
 // A checkout switch invalidates the old intent even when the new request fails.
 await page.fill('#checkout-id','missing-checkout');
 assert(await page.locator('#offers-panel').isHidden()&&await page.locator('#apply-button').isDisabled(),'editing checkout immediately invalidates the previous selectable quote');
 await page.click('#quote-button');
 await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('Checkout not found'));
 assert(await page.locator('#offers-panel').isHidden()&&await page.locator('#status-panel').isHidden()&&await page.locator('#apply-button').isDisabled(),'failed checkout switch cannot retain or apply the prior basket quote');
 await page.fill('#checkout-id','reference-checkout');await page.click('#quote-button');await page.waitForSelector('.offer-card');
 // Changing credentials removes prior-customer data before authorization succeeds.
 await page.fill('#customer-token',PUBLIC_OTHER_FIXTURE_TOKEN);
 assert(await page.locator('#offers-panel').isHidden(),'credential edit immediately removes the previous customer quote');
 await page.click('#quote-button');
 await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('Checkout not found'));
 assert(await page.locator('#apply-button').isDisabled()&&await page.locator('#offers-panel').isHidden(),'another valid customer cannot reuse the prior customer selection after an ownership refusal');
 await page.fill('#customer-token',PUBLIC_FIXTURE_TOKEN);await page.click('#quote-button');await page.waitForSelector('.offer-card');
 await page.route('**/v1/quotes',async route=>{const response=await route.fetch();const data=await response.json();data.decisions[0].expected_benefit_paise=data.decisions[0].nominal_benefit_paise+1;await route.fulfill({response,json:data});});
 await page.click('#quote-button');
 await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('invalid quote'));
 assert(await page.locator('#offers-panel').isHidden()&&await page.locator('#apply-button').isDisabled(),'malformed quote amounts fail closed instead of showing a stale or misleading offer');
 await page.unroute('**/v1/quotes');await page.click('#quote-button');await page.waitForSelector('.offer-card');
 let abortFirst=true;const keys=[],requestBodies=[],credentials=[];
 await page.route('**/v1/applications',async route=>{
  keys.push(route.request().headers()['idempotency-key']);requestBodies.push(route.request().postDataJSON());credentials.push(route.request().headers()['authorization']);
  if(abortFirst){abortFirst=false;await route.fetch();await route.abort('failed');}else await route.continue();
 });
 await page.click('#apply-button');
 await page.waitForFunction(()=>document.querySelector('#apply-button').textContent.includes('Retry the same application'));
 await page.waitForFunction(()=>!document.querySelector('#apply-button').disabled);
 assert(await page.locator('input[name=offer]:disabled').count()===2,'ambiguous apply locks offer changes');
 assert(await page.locator('#customer-token').isDisabled()&&await page.locator('#checkout-id').isDisabled(),'ambiguous application freezes credential and checkout controls until the exact retry resolves');
 await page.click('#apply-button');
 await page.waitForSelector('#status-panel:not([hidden])');
 assert(keys.length===2&&keys[0]===keys[1]&&keys[0].length===32,'retry reuses the exact idempotency key after server accepted the first request');
 assert(JSON.stringify(requestBodies[0])===JSON.stringify(requestBodies[1])&&credentials[0]===credentials[1],'ambiguous retry retains the exact quote, offer and credential as well as the key');
 assert(await page.locator('#application-state').textContent()==='HELD','successful retry retrieves the same held application');
 await fetch(base+'/_qa/capture',{method:'POST',headers:{'Content-Type':'application/json',Authorization:`Bearer ${PUBLIC_FIXTURE_TOKEN}`},body:'{}'});
 await page.click('#refresh-button');
 await page.waitForFunction(()=>document.querySelector('#application-state').textContent==='OWED');
 assert(await page.locator('#state-explanation').textContent().then(t=>t.includes('separate matching delivery evidence')),'captured payment visibly remains owed until separate benefit evidence');
 await page.route('**/v1/applications/*',async route=>{await route.fulfill({status:200,contentType:'application/json',json:{application_id:'incomplete-response',state:'FULFILLED'}});});
 await page.click('#refresh-button');await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('invalid application result'));
 assert(await page.locator('#application-state').textContent()==='OWED','malformed status response preserves the previous verified owed state');
 await page.unroute('**/v1/applications/*');
 await page.route('**/v1/applications/*',async route=>{const response=await route.fetch();const data=await response.json();data.application_id='another-application';data.state='FULFILLED';data.benefit_state='FULFILLED';await route.fulfill({response,json:data});});
 await page.click('#refresh-button');await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('invalid application result'));
 assert(await page.locator('#application-state').textContent()==='OWED','a valid-shaped response for another application cannot replace this checkout outcome');
 await page.unroute('**/v1/applications/*');
 await page.screenshot({path:path.join(out,'checkout-desktop-owed.png'),fullPage:true});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),'desktop has no horizontal overflow');
 await fetch(base+'/_qa/benefit',{method:'POST',headers:{'Content-Type':'application/json',Authorization:`Bearer ${PUBLIC_FIXTURE_TOKEN}`},body:'{}'});
 await page.click('#refresh-button');
 await page.waitForFunction(()=>document.querySelector('#application-state').textContent==='FULFILLED');
 assert(await page.locator('#status-facts').textContent().then(t=>t.includes('COMMITTED')),'fulfillment leaves campaign funding committed');
 await page.setViewportSize({width:390,height:844});
 await page.screenshot({path:path.join(out,'checkout-mobile-fulfilled.png'),fullPage:true});
 const outcome=await fetch(base+'/_qa/status',{headers:{Authorization:`Bearer ${PUBLIC_FIXTURE_TOKEN}`}}).then(response=>response.json());
 assert(outcome.application_count===1&&outcome.payment_count===1&&outcome.benefit_obligation_count===1&&outcome.funding_conserved,'one application, payment and benefit obligation survive retry with conserved funding');
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),'390px mobile has no horizontal overflow');
 assert(await page.locator('#refresh-button').isVisible(),'mobile status action remains visible');
 await page.fill('#customer-token',PUBLIC_OTHER_FIXTURE_TOKEN);
 assert(await page.locator('#status-panel').isHidden()&&await page.locator('#refresh-button').isDisabled(),'credential switch removes the previous customer application status and refresh capability');
 await page.click('#quote-button');await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('Checkout not found'));
 assert(await page.locator('#status-panel').isHidden(),'failed customer switch does not restore private status from the previous session');
 await page.click('#forget-button');
 assert(await page.inputValue('#customer-token')===''&&await page.inputValue('#checkout-id')==='','reset clears customer inputs');
 assert(await page.locator('#offers-panel').isHidden()&&await page.locator('#status-panel').isHidden(),'reset removes quote and outcome from the page');
 assert(await page.locator('#message').textContent().then(t=>t.includes('does not cancel')),'reset explains that server-side financial state is retained');
 await page.fill('#checkout-id','reference-checkout');
 await page.fill('#customer-token',PUBLIC_FIXTURE_TOKEN);
 await page.route('**/v1/quotes',async route=>{
  const response=await route.fetch();const data=await response.json();data.decisions[0].issuer='<img src=x onerror="window.injected=true">';
  await route.fulfill({response,json:data});
 });
 await page.click('#quote-button');
 await page.waitForSelector('.offer-card');
 assert(await page.locator('.offer-card img').count()===0&&!(await page.evaluate(()=>window.injected)),'dynamic issuer text cannot inject HTML');
 assert(errors.length===0,'no uncaught browser errors');
 assert(JSON.stringify(sourceAtStart)===JSON.stringify(sourceHashes()),'UI, API and harness source hashes remained unchanged throughout the browser run');
 fs.writeFileSync(path.join(out,'ui-verification.json'),JSON.stringify({browser:browser.version(),playwright:require('playwright/package.json').version,source_sha256:sourceAtStart,source_files_hashed:Object.keys(sourceAtStart).length,source_scope:"All src/festive_offers Python modules, the dataset fixture, three web assets and both browser harness files; hashes checked before and after execution",scope:'Real Chrome browser against loopback OfferApi + SQLite + synthetic provider; no live payments',viewport_desktop:{width:1280,height:960},viewport_mobile:{width:390,height:844},checks,uncaught_browser_errors:errors,screenshots:['checkout-desktop-owed.png','checkout-mobile-fulfilled.png']},null,2)+'\n');
 await browser.close();activeBrowser=null;
 for(const name of ["checkout-desktop-owed.png", "checkout-mobile-fulfilled.png", "ui-verification.json"]) fs.copyFileSync(path.join(out,name),path.join(destination,name));
 console.log(JSON.stringify({checks:checks.length,errors:errors.length,screenshots:2,output:destination}));
})().catch(error => { console.error(error.message); process.exitCode = 1; }).finally(cleanup);
