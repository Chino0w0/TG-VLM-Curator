// Real Chrome + real local HTTP routes/services. Only fault/delay and serializer replay routes are intercepted.
const fs = require('fs'), path = require('path'), assert = require('node:assert/strict');
function loadPlaywrightCore() {
  const target = process.env.PLAYWRIGHT_CORE || 'playwright-core';
  try {
    return require(target);
  } catch (err) {
    throw new Error(`Unable to load '${target}'. Install playwright-core or set PLAYWRIGHT_CORE=/path/to/playwright-core. Original error: ${err.message}`);
  }
}
function resolveGitCommonDir(repoRoot) {
  const dotGit = path.join(repoRoot, '.git');
  if (fs.existsSync(dotGit) && fs.statSync(dotGit).isFile()) {
    const content = fs.readFileSync(dotGit, 'utf8').trim();
    if (content.startsWith('gitdir:')) {
      const rawGitDir = content.slice('gitdir:'.length).trim();
      const gitDir = path.resolve(repoRoot, rawGitDir);
      const commonDirFile = path.join(gitDir, 'commondir');
      if (fs.existsSync(commonDirFile)) {
        return path.resolve(gitDir, fs.readFileSync(commonDirFile, 'utf8').trim());
      }
      return gitDir;
    }
  }
  return dotGit;
}
const {chromium} = loadPlaywrightCore();
const repoRoot = path.resolve(__dirname, '../..');
const evidence = path.resolve(process.argv[2] || path.join(resolveGitCommonDir(repoRoot), 'fix-2026-11-12/pr30'));
const base = process.env.TEST_BASE_URL || 'http://127.0.0.1:18731';
assert.equal(new URL(base).hostname, '127.0.0.1', 'Browser regression only permits an explicitly local server');
const fixture = JSON.parse(fs.readFileSync(path.join(evidence,'fixture.json'),'utf8'));
const native = process.env.TEST_NATIVE_POSTGRES === '1';
const username = process.env.TEST_USERNAME || 'curator-admin';
const password = process.env.TEST_PASSWORD || 'correct-horse-battery-staple';
const result = {boundary:native ? 'Real migration-created PostgreSQL + SqlAlchemyAdminBackend + encrypted SQL vault; no InMemory or DTO replay; no workers/external calls/deletion.' : 'Local real FastAPI auth/routes/services; InMemory persistence. Serializer replay identified separately. No workers/external providers/production.', checks: [], errors: []};
const prefix = `chrome-${Date.now()}`;
let browser, context, page, csrf;
const save = () => fs.writeFileSync(path.join(evidence,'browser-results.json'),JSON.stringify(result,null,2));
const check = async (name, fn) => { const data = await fn(); result.checks.push({name, pass:true, ...data}); save(); console.log('PASS',name); };
const nav = async name => { await page.getByRole('navigation').getByRole('button',{name,exact:true}).click(); };
const post = async (url,data={}) => {const response = await context.request.post(base+url,{headers:{'X-CSRF-Token':csrf},data});assert.ok(response.ok(),`${response.status()} ${await response.text()}`);return response.json();};
const get = async url => {const response=await context.request.get(base+url);assert.ok(response.ok());return response.json();};
const clickResponse = async (button, suffix, method='POST') => {const response=page.waitForResponse(r=>r.url().endsWith(suffix)&&r.request().method()===method);await button.click();const res=await response;assert.ok(res.ok(),`${res.status()} ${await res.text()}`);return {body:await res.json(),request:res.request().postDataJSON()};};
const shot = name => page.screenshot({path:path.join(evidence,name+'.png'),fullPage:true});
const settle = () => page.waitForTimeout(120);
(async()=>{
 try {
  browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',args:['--disable-background-networking','--disable-component-update','--no-first-run']});
  context=await browser.newContext({viewport:{width:1440,height:1080}});
  await context.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
  page=await context.newPage();page.setDefaultTimeout(10000);page.on('pageerror',e=>{result.errors.push(e.message);save();});
  await page.goto(base);await page.locator('#admin-username').fill(username);await page.locator('#admin-current-password').fill(password);await page.getByRole('button',{name:'Sign In',exact:true}).click();await page.getByRole('navigation').waitFor();csrf=(await get('/api/v1/auth/me')).csrf_token;
  const created={};
  async function createAnalysis(family,tab,prepare) {
    await nav('Analysis & Test Bench');await page.getByRole('tab',{name:tab,exact:true}).click();await settle();
    const form=page.locator('form').filter({has:page.locator('#new-cfg-name')});
    await page.locator('#new-cfg-name').fill(`${prefix}-${family}`);if(prepare)await prepare(form);
    const response=await clickResponse(form.getByRole('button',{name:'Create Entity (DRAFT v1)',exact:true}),'/api/v1/config/'+family);
    assert.ok(response.request.initial_version);assert.equal(response.body.versions.length,1);
    const ent=response.body,ver=ent.versions[0];created[family]={entity:ent.id,version:ver.id,content:ver.content};
    await page.locator('#cfg-entity-select').selectOption(ent.id);await settle();
    await clickResponse(page.getByRole('button',{name:'Publish Version',exact:true}),`/versions/${ver.id}/publish`);
    return response;
  }
  await check('M9-02 label initial_version and default schema; M9-10 dirty draft blocked',async()=>{
    await nav('Analysis & Test Bench');await page.locator('#new-cfg-name').fill(prefix+'-label');
    const res=await clickResponse(page.getByRole('button',{name:'Create Entity (DRAFT v1)',exact:true}),'/api/v1/config/label-definitions');
    assert.ok(res.request.initial_version.display_name);assert.equal(res.body.versions.length,1);
    const ent=res.body,ver=ent.versions[0];created['label-definitions']={entity:ent.id,version:ver.id};
    await page.locator('#cfg-entity-select').selectOption(ent.id);await settle();
    const content={...ver.content,description:'SAVED THROUGH UI'};await page.locator('#cfg-version-editor').fill(JSON.stringify(content));
    assert.equal(await page.getByRole('button',{name:'Publish Version',exact:true}).isDisabled(),true);
    await shot('01-dirty-publish-locked');
    await clickResponse(page.getByRole('button',{name:'Save Draft',exact:true}),`/versions/${ver.id}`,'PATCH');
    await clickResponse(page.getByRole('button',{name:'Publish Version',exact:true}),`/versions/${ver.id}/publish`);
    const actual=(await get('/api/v1/config/label-definitions')).items.find(e=>e.id===ent.id);assert.equal(actual.versions[0].content.description,'SAVED THROUGH UI');
    return {entity:ent.id,version:ver.id,description:actual.versions[0].content.description};
  });
  await check('M9-02 empty analysis entity first-draft recovery',async()=>{
    const empty=await post('/api/v1/config/label-definitions',{name:prefix+'-empty'});
    await nav('Dashboard');await nav('Analysis & Test Bench');await page.locator('#cfg-entity-select').selectOption(empty.id);
    if(native){const draft=JSON.parse(await page.locator('#new-cfg-json').inputValue());draft.key='recovered_label';await page.locator('#new-cfg-json').fill(JSON.stringify(draft));}
    const res=await clickResponse(page.getByRole('button',{name:'Create First Draft',exact:true}),`/config/label-definitions/${empty.id}/versions`);assert.equal(res.body.version_number,1);return {version:res.body.id};
  });
  await check('M9-02 published-reference configuration chain',async()=>{
    await createAnalysis('label-sets','Label Sets',form=>form.getByLabel('label_definition_version_id',{exact:true}).selectOption(created['label-definitions'].version));
    await createAnalysis('prompt-templates','Prompt Templates');
    await createAnalysis('inference-profiles','Inference Profiles',form=>form.getByLabel('api_secret_reference',{exact:true}).selectOption(fixture.inference_secret_id));
    await createAnalysis('analysis-stages','Analysis Stages',async form=>{for(const [key,family]of [['prompt_template_version_id','prompt-templates'],['inference_profile_version_id','inference-profiles'],['label_set_version_id','label-sets']])await form.getByLabel(key,{exact:true}).selectOption(created[family].version);});
    await createAnalysis('analysis-pipelines','Analysis Pipelines',form=>form.getByLabel('analysis_stage_template_version_id',{exact:true}).selectOption(created['analysis-stages'].version));
    await createAnalysis('media-processing-profiles','Media Profiles');
    return {families:created};
  });
  await check('M9-02 rendering/routing defaults, dirty routing guard and archive entry',async()=>{
    const empty=await post('/api/v1/config/rendering-templates',{name:prefix+'-empty-template'});
    await nav('Routing & Destinations');await page.getByRole('tab',{name:'Rendering Templates',exact:true}).click();await page.locator('#rt-entity-select').selectOption(empty.id);const recovered=await clickResponse(page.getByRole('button',{name:'Create First Draft',exact:true}),`/config/rendering-templates/${empty.id}/versions`);assert.equal(recovered.body.version_number,1);created['default-rendering']={entity:empty.id,version:recovered.body.id,content:recovered.body.content};await page.locator('#rt-new-name').fill(prefix+'-template');
    const res=await clickResponse(page.getByRole('button',{name:'Create Draft v1',exact:true}),'/api/v1/config/rendering-templates');assert.ok(res.request.initial_version.template_text);
    const ent=res.body,ver=ent.versions[0];created['rendering-templates']={entity:ent.id,version:ver.id};await page.locator('#rt-entity-select').selectOption(ent.id);await settle();
    await page.locator('#rt-json-editor').fill(JSON.stringify({template_text:'Saved {{message.original_text}}'}));assert.equal(await page.getByRole('button',{name:'Publish Version',exact:true}).isDisabled(),true);
    await clickResponse(page.getByRole('button',{name:'Save Draft',exact:true}),`/versions/${ver.id}`,'PATCH');await clickResponse(page.getByRole('button',{name:'Publish Version',exact:true}),`/versions/${ver.id}/publish`);
    await clickResponse(page.getByRole('button',{name:'Archive Version',exact:true}),`/versions/${ver.id}/archive`);
    await page.getByRole('tab',{name:'Routing Policies',exact:true}).click();await page.locator('#rt-new-name').fill(prefix+'-policy');
    const form=page.locator('form').filter({has:page.locator('#rt-new-name')});await form.getByLabel('destination_channel_id',{exact:true}).selectOption(fixture.destination_id);
    const pol=await clickResponse(page.getByRole('button',{name:'Create Draft v1',exact:true}),'/api/v1/config/routing-policies');const pv=pol.body.versions[0];created['routing-policies']={entity:pol.body.id,version:pv.id};await page.locator('#rt-entity-select').selectOption(pol.body.id);await settle();await clickResponse(page.getByRole('button',{name:'Publish Version',exact:true}),`/versions/${pv.id}/publish`);
    assert.equal(pv.content.rules[0].actions[0].publication_mode,'forward_only');await shot('02-routing-published');return {template:ver.id,policy:pv.id};
  });
  await check('Default routing predicate and rendering template execute against real facts',async()=>{
    const template=created['default-rendering'];await post(`/api/v1/config/rendering-templates/${template.entity}/versions/${template.version}/publish`);
    const mid=fixture.message_ids[0];await post('/api/v1/messages/'+mid+'/labels',{operation:'set',target_scope:'global',target_kind:'message',target_id:mid,label_definition_version_id:created['label-definitions'].version,label_key:'illustration',score:0.9,activated:true});
    const policy=created['routing-policies'];const entity=(await get('/api/v1/config/routing-policies')).items.find(e=>e.id===policy.entity);const content=structuredClone(entity.versions[0].content);content.rules[0].actions[0].publication_mode='copy_with_caption';content.rules[0].actions[0].rendering_template_version_id=template.version;
    const version=await post(`/api/v1/config/routing-policies/${policy.entity}/versions`,content);await post(`/api/v1/config/routing-policies/${policy.entity}/versions/${version.id}/publish`);
    const result=await post('/api/v1/messages/'+mid+'/reroute',{routing_policy_version_id:version.id,dry_run:true,reason:'Local default predicate/template regression'});assert.equal(result.intents.length,1);assert.equal(result.intents[0].rendered_text,"Test message 01");assert.equal(result.persisted,false);fs.writeFileSync(path.join(evidence,"reroute-dto.json"),JSON.stringify(result,null,2));created["routing-policies"].version=version.id;return {matched_rule_ids:result.matched_rule_ids,planned:result.intents,template:template.content};
  });
  await check('M9-04 published profile binds only its own source',async()=>{
    await createAnalysis('source-channel-profiles','Channel Profiles',async form=>{await form.locator('#new-cfg-channel').selectOption(fixture.source_id);for(const [key,family]of [['analysis_pipeline_version_id','analysis-pipelines'],['media_processing_profile_version_id','media-processing-profiles'],['routing_policy_version_id','routing-policies']])await form.getByLabel(key,{exact:true}).selectOption(created[family].version);});
    await nav('Source Channels');const vid=created['source-channel-profiles'].version;
    await page.locator(`[id="assign-prof-${fixture.source_id}"]`).selectOption(vid);await settle();
    assert.equal((await get('/api/v1/source-channels')).items.find(x=>x.id===fixture.source_id).active_profile_version_id,vid);
    assert.equal(await page.locator(`[id="assign-prof-${fixture.other_source_id}"] option[value="${vid}"]`).count(),0);await shot('03-profile-bound');return {version:vid};
  });  await check('M9-06 exact processing/duplicate enums and M9-07 stale Messages page discarded',async()=>{
    await nav('Messages');await page.locator('#msg-filter-proc').selectOption('processed');await page.getByRole('button',{name:'Apply Filters',exact:true}).click();
    const table=page.getByRole('table',{name:/message/i});await page.waitForFunction(()=>document.querySelectorAll('tbody tr').length===25);
    assert.deepEqual(await page.locator('#msg-filter-proc option').evaluateAll(es=>es.map(e=>e.value)),['','ingested','processing','processed','failed']);
    assert.ok((await page.locator('#msg-filter-dedup option').evaluateAll(es=>es.map(e=>e.value))).includes('duplicate_reclassify'));
    let release, startedResolve;const started=new Promise(r=>startedResolve=r);
    const handler=async route=>{if(new URL(route.request().url()).searchParams.has('cursor')){const response=await route.fetch();startedResolve();await new Promise(r=>release=r);await route.fulfill({response});}else await route.continue();};
    await page.route('**/api/v1/messages?**',handler);await page.getByRole('button',{name:'Next Page',exact:true}).click();await started;
    await page.locator('#msg-filter-query').fill('Test message 01');await page.getByRole('button',{name:'Apply Filters',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('tbody tr').length===1);
    release();await settle();assert.equal(await table.locator('tbody tr').count(),1);assert.ok((await table.innerText()).includes('Test message 01'));assert.equal(await page.getByRole('button',{name:'Next Page',exact:true}).isDisabled(),true);await page.unroute('**/api/v1/messages?**',handler);await shot('04-latest-query-retained');return {rows:1,query:'Test message 01'};
  });
  await check('M9-09 Review cursor pagination reaches all 30 records',async()=>{
    await nav('Review & Overrides');const table=page.getByRole('table',{name:'Messages available for human review'});await page.waitForFunction(()=>document.querySelectorAll('tbody tr').length===25);
    await page.getByRole('button',{name:'Next Queue Page',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('tbody tr').length===5);assert.equal(await table.locator('tbody tr').count(),5);await shot('05-review-second-page');await page.getByRole('button',{name:'Previous Queue Page',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('tbody tr').length===25);return {pages:[25,5]};
  });
  await check('M9-05 image/video set and clear have exact asset identity',async()=>{
    const events=[];
    for(const kind of ['image_asset','video_asset'])for(const operation of ['set','clear']){
      await page.locator('#rev-scope').selectOption('media');await page.locator('#rev-op').selectOption(operation);await page.locator('#rev-label-def').selectOption(fixture.media_label_version_id);
      const value=await page.locator(`#rev-target-id option[value^="${kind}:"]`).first().getAttribute('value');await page.locator('#rev-target-id').selectOption(value);
      const response=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/labels'));await page.getByRole('button',{name:'Record Manual Label Event',exact:true}).click();const res=await response;assert.ok(res.ok(),await res.text());const body=res.request().postDataJSON();assert.equal(body.target_kind,kind);assert.equal(body[kind==='image_asset'?'image_asset_id':'video_asset_id'],body.target_id);assert.ok(!body[kind==='image_asset'?'video_asset_id':'image_asset_id']);events.push({operation,kind,status:res.status(),target:body.target_id});await page.locator('#rev-scope').waitFor();await settle();
    }
    await shot('06-media-events');return {events};
  });
  if(native) await check('Native MEDIA set/clear drives actual effective/manual SQL filtering',async()=>{
    const mid=fixture.message_ids[29], target=(await get('/api/v1/messages/'+mid)).media.images[0].id;
    await post('/api/v1/messages/'+mid+'/labels',{operation:'set',target_scope:'media',target_kind:'image_asset',target_id:target,image_asset_id:target,label_definition_version_id:fixture.media_label_version_id,label_key:'media_quality',score:1,activated:true});
    await nav('Messages');await page.locator('#msg-filter-label-key').fill('media_quality');await page.locator('#msg-filter-label-ns').selectOption('effective');await page.locator('#msg-filter-label-scope').selectOption('media');await page.getByRole('button',{name:'Apply Filters',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('tbody tr').length===1);assert.ok((await page.locator('tbody').innerText()).includes('Test message 30'));
    await page.locator('#msg-filter-label-ns').selectOption('manual');await page.getByRole('button',{name:'Apply Filters',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('tbody tr').length===1);
    await post('/api/v1/messages/'+mid+'/labels',{operation:'clear',target_scope:'media',target_kind:'image_asset',target_id:target,image_asset_id:target,label_definition_version_id:fixture.media_label_version_id,label_key:'media_quality'});await page.getByRole('button',{name:'Apply Filters',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('tbody tr').length===0);await page.locator('#msg-filter-label-ns').selectOption('effective');await page.getByRole('button',{name:'Apply Filters',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('tbody tr').length===0);await shot('native-media-filter-clear');await nav('Review & Overrides');await page.locator('#rev-reroute-policy').waitFor();return {setEffectiveRows:1,setManualRows:1,clearManualRows:0,clearEffectiveRows:0};
  });
  await check('Review renders real reroute comparison DTO without white screen',async()=>{
    await page.locator('#rev-reroute-policy').selectOption(created['routing-policies'].version);const response=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/reroute'));await page.getByRole('button',{name:'Preview Dry-Run Re-route',exact:true}).click();const res=await response;assert.ok(res.ok(),await res.text());await settle();assert.ok((await page.locator('main').innerText()).includes('Previous Evaluation:'));assert.equal(await page.getByRole('navigation').count(),1);return {status:res.status(),response:await res.json()};
  });
  await check('M9-01 old cleanup preview cannot unlock; confirmation shows exact scope and is single-shot',async()=>{
    await nav('Archive Cleanup');await page.locator('#cln-archived-before').fill('2026-12-01T00:00:00Z');let release,startedResolve;const started=new Promise(r=>startedResolve=r);
    const handler=async route=>{if(route.request().method()==='POST'){const response=await route.fetch();startedResolve();await new Promise(r=>release=r);await route.fulfill({response});}else await route.continue();};
    await page.route('**/api/v1/archive-cleanup/dry-runs',handler);await page.getByRole('button',{name:'Step 1: Run Dry-Run Preview',exact:true}).click();await started;await page.locator('#cln-archived-before').fill('2026-10-01T00:00:00Z');release();await settle();assert.equal(await page.getByRole('button',{name:'Step 2: Confirm Cleanup Job...',exact:true}).isDisabled(),true);await shot('07-stale-cleanup-locked');await page.unroute('**/api/v1/archive-cleanup/dry-runs',handler);
    await page.locator('#cln-archived-before').fill('2026-12-01T00:00:00Z');await clickResponse(page.getByRole('button',{name:'Step 1: Run Dry-Run Preview',exact:true}),'/api/v1/archive-cleanup/dry-runs');await page.getByRole('button',{name:'Step 2: Confirm Cleanup Job...',exact:true}).click();const dialog=page.getByRole('dialog');assert.ok((await dialog.innerText()).includes('2026-12-01'));assert.ok((await dialog.innerText()).includes('Archived before (exclusive UTC)'));await shot('08-cleanup-confirm-scope');
    let submits=0;page.on('request',r=>{if(r.method()==='POST'&&r.url().endsWith('/archive-cleanup/jobs'))submits++;});const response=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/archive-cleanup/jobs'));await dialog.getByRole('button',{name:'Delete Matched Archive Blobs',exact:true}).evaluate(button=>{button.click();button.click();});const res=await response;assert.ok(res.ok(),await res.text());await settle();assert.equal(submits,1);assert.equal(await page.getByRole('button',{name:'Step 2: Confirm Cleanup Job...',exact:true}).isDisabled(),true);return {submits,job:await res.json(),no_worker_executed:true};
  });
  if(native) await check('M9-03 native PostgreSQL detail renders stored Manifest/InferenceCall/evaluation/intents',async()=>{
    const mid=fixture.message_ids[0];const dto=await get('/api/v1/messages/'+mid);assert.ok(dto.analysis_runs.length);assert.ok(dto.routing_evaluations.length);assert.ok(dto.publication_intents.length);assert.ok(dto.analysis_runs[0].stage_runs[0].input_manifest.input_manifest_hash);
    fs.writeFileSync(path.join(evidence,'native-detail-http.json'),JSON.stringify(dto,null,2));await nav('Message Detail');await page.locator('#detail-lookup-id').fill(mid);await page.getByRole('button',{name:'Load Causal Chain',exact:true}).click();await page.getByText('Manifest & inference diagnostics',{exact:true}).waitFor();await page.getByText('Manifest & inference diagnostics',{exact:true}).click();assert.ok((await page.locator('main').innerText()).includes('input_tokens'));assert.ok((await page.locator('main').innerText()).includes(fixture.diagnostic_evaluation_id));assert.equal(await page.getByRole('navigation').count(),1);assert.equal(result.errors.length,0);await shot('09-native-postgres-chain');return {analysis_runs:dto.analysis_runs.length,evaluations:dto.routing_evaluations.length,intents:dto.publication_intents.length,no_replay:true};
  });
  else {
  await check('M9-03 actual DB serializers render full causal chain, records, manifest/token diagnostics',async()=>{
    const dto=JSON.parse(fs.readFileSync(path.join(evidence,'serializer-detail.json'),'utf8'));const url='**/api/v1/messages/'+dto.message.id;await page.route(url,route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(dto)}));await nav('Message Detail');await page.locator('#detail-lookup-id').fill(dto.message.id);await page.getByRole('button',{name:'Load Causal Chain',exact:true}).click();await page.getByText('primary:#9001, #9002',{exact:true}).waitFor();await page.getByText('Manifest & inference diagnostics',{exact:true}).click();assert.ok((await page.locator('main').innerText()).includes('input_tokens'));assert.equal(await page.getByRole('navigation').count(),1);assert.equal(result.errors.length,0);await shot('09-real-serializer-chain');await page.unroute(url);return {source:'actual current _serialize_stage_run/_serialize_analysis_run/_serialize_evaluation/_serialize_intent',remoteRecords:[9001,9002],browserErrors:result.errors};
  });
  }
  await check('Test Bench diagnostics and safe no-provider mode',async()=>{
    await nav('Analysis & Test Bench');await page.getByRole('tab',{name:'Prompt Test Bench',exact:true}).click();await page.locator('select#bench-stage-ver').selectOption(created['analysis-stages'].version);await page.locator('#bench-invoke-provider').uncheck();const res=await clickResponse(page.getByRole('button',{name:'Execute Prompt Test Bench',exact:true}),'/api/v1/analysis/test-bench');assert.equal(res.body.run_mode,'test');assert.ok((await page.locator('main').innerText()).includes('token_usage'));await shot('10-bench-diagnostics');return {status:res.body.status,invoke_provider:res.request.invoke_provider};
  });
  await check('Identity enable/edit and channel identity adjustment APIs accessible',async()=>{
    await nav('Identities & Secrets');await page.getByRole('button',{name:'Edit Identity',exact:true}).first().click();const form=page.getByRole('form',{name:'Edit Identity'});await form.getByLabel('Identity Name',{exact:true}).fill('Edited test reader');await form.getByLabel('Enabled',{exact:true}).uncheck();await clickResponse(form.getByRole('button',{name:'Save Identity',exact:true}),'/telegram-identities/'+fixture.identity_id,'PATCH');assert.equal((await get('/api/v1/telegram-identities')).items.find(x=>x.id===fixture.identity_id).enabled,false);
    await page.getByRole('button',{name:'Edit Identity',exact:true}).first().click();await page.getByRole('form',{name:'Edit Identity'}).getByLabel('Enabled',{exact:true}).check();await clickResponse(page.getByRole('button',{name:'Save Identity',exact:true}),'/telegram-identities/'+fixture.identity_id,'PATCH');
    await nav('Source Channels');const row=page.locator('tr').filter({has:page.locator(`[id="assign-prof-${fixture.source_id}"]`)});await row.getByRole('button',{name:'Edit Source',exact:true}).click();const sourceForm=page.getByRole('form',{name:'Edit Source'});await sourceForm.getByLabel('Display Name',{exact:true}).fill('Edited Source');await sourceForm.getByLabel('Read Identity',{exact:true}).selectOption(fixture.identity_id);await clickResponse(sourceForm.getByRole('button',{name:'Save Source',exact:true}),'/source-channels/'+fixture.source_id,'PATCH');
    await nav('Routing & Destinations');await page.getByRole('button',{name:'Edit Destination',exact:true}).first().click();const destinationForm=page.getByRole('form',{name:'Edit Destination'});await destinationForm.getByLabel('Display Name',{exact:true}).fill('Edited Destination');await destinationForm.getByLabel('Publish Identity',{exact:true}).selectOption(fixture.identity_id);await clickResponse(destinationForm.getByRole('button',{name:'Save Destination',exact:true}),'/destination-channels/'+fixture.destination_id,'PATCH');await shot('11-destination-edited');return {identityReenabled:true,sourceEdited:true,destinationEdited:true};
  });
  await check('Audit stale page ignored and mobile layout remains usable',async()=>{
    await nav('Audit Log');await page.waitForFunction(()=>document.querySelectorAll('tbody tr').length===30);
    let release,startedResolve;const started=new Promise(r=>startedResolve=r);const handler=async route=>{if(new URL(route.request().url()).searchParams.has('cursor')){const response=await route.fetch();startedResolve();await new Promise(r=>release=r);await route.fulfill({response});}else await route.continue();};
    await page.route('**/api/v1/audit-events?**',handler);await page.getByRole('button',{name:'Next Page',exact:true}).click();await started;await page.locator('#audit-event-type').fill('NO_SUCH_EVENT');await page.locator('#audit-event-type').press('Enter');await page.waitForFunction(()=>document.querySelectorAll('tbody tr').length===0);release();await settle();assert.equal(await page.locator('tbody tr').count(),0);assert.equal(await page.getByRole('button',{name:'Previous Page',exact:true}).isDisabled(),true);await page.unroute('**/api/v1/audit-events?**',handler);
    await page.setViewportSize({width:390,height:844});const width=await page.evaluate(()=>document.documentElement.scrollWidth);assert.ok(width<=390);await shot('14-mobile-audit');await page.setViewportSize({width:1440,height:1080});return {rows:0,mobileDocumentWidth:width};
  });
  await check('M9-08 logout failure retains accurate session; 401 returns login; successful logout revokes',async()=>{
    if(!native) {
    const handler=route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:{code:'test_unavailable',message:'Injected local logout failure'}})});await page.route('**/api/v1/auth/logout',handler);await page.getByRole('button',{name:'Sign Out',exact:true}).click();await page.getByRole('alert').filter({hasText:'server session may still be active'}).waitFor();assert.equal(await page.getByRole('navigation').count(),1);assert.ok((await get('/api/v1/auth/me')).user);await shot('12-logout-failure-honest');await page.unroute('**/api/v1/auth/logout',handler);
    }
    await context.clearCookies();await nav('Messages');await page.locator('#admin-username').waitFor();assert.equal(await page.getByRole('navigation').count(),0);await shot('13-expired-session-login');await page.locator('#admin-username').fill(username);await page.locator('#admin-current-password').fill(password);await page.getByRole('button',{name:'Sign In',exact:true}).click();await page.getByRole('navigation').waitFor();await page.getByRole('button',{name:'Sign Out',exact:true}).click();await page.locator('#admin-username').waitFor();assert.equal((await context.request.get(base+'/api/v1/auth/me')).status(),401);return {failureRetained:native ? "not injected in native mode" : true,expiredLogin:true,successRevoked:true};
  });
  assert.equal(result.errors.length,0);result.complete=true;
 } catch(error) {result.failure=String(error.stack).split(password).join('[REDACTED_TEST_PASSWORD]');console.error(result.failure);if(page)await shot('failure').catch(()=>{});process.exitCode=1;}
 finally {save();if(context)await context.close();if(browser)await browser.close();}
})();
