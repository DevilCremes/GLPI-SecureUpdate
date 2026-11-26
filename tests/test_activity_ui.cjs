// Run with node --test tests/test_activity_ui.cjs. No GLPI session or network is required.
const test = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const {resolve} = require('node:path');
const code = readFileSync(resolve(__dirname, '../secureupdate/public/js/panel.js'), 'utf8');

function fixture(replies, initiallyBusy = true) {
    const element = () => ({
        textContent:'', hidden:false, children:[], events:{}, dataset:{},
        classList:{toggle(){}, remove(){}}, scrollIntoView(){},
        append(node){ this.children.push(node); }, replaceChildren(){ this.children = []; },
        removeAttribute(name){ delete this[name]; },
        addEventListener(name, cb){ this.events[name] = cb; }
    });
    const parts = Object.fromEntries(['details','ol','strong','progress','.gu-meter-text','.gu-connection','.gu-request-feedback']
        .map(selector => [selector, element()]));
    parts['.gu-request-feedback'].hidden = true;
    const monitor = Object.assign(element(), {hidden:!initiallyBusy, dataset:{url:'/panel?andamento=1'}, querySelector:s=>parts[s]});
    const next = {dataset:{tab:'backups',busy:'0',job:'j'}};
    const current = {dataset:{tab:'backups'},replaceWith(node){ assert.equal(node, next); this.replaced = true; }};
    const document = Object.assign(element(), {
        querySelectorAll:s=>s === '.gu-monitor' ? [monitor] : [], querySelector:s=>s === '.gu-monitor .gu-request-feedback' ? parts['.gu-request-feedback'] : current,
        createElement:element, createTextNode:text=>text, importNode:node=>node
    });
    const pending = new Map();
    let tid = 0;
    const requests = [];
    const session = new Map([['secureupdate.console.open','no']]);
    let xhr;
    const location = {href:'http://glpi.test/panel?aba=backups#gu-operacoes',origin:'http://glpi.test'};
    runInNewContext(code, {
        document, window:element(), location, URL, AbortController,
        sessionStorage:{getItem:k=>session.get(k),setItem:(k,v)=>session.set(k,v)},
        setTimeout(cb, delay){pending.set(++tid, {cb, delay}); return tid;}, clearTimeout(id){pending.delete(id);},
        async fetch(url, options){
            assert.equal(options.cache,'no-store');
            requests.push(String(url));
            const response = replies.shift();
            if (response instanceof Error) throw response;
            assert.ok(response, 'unexpected poll');
            return {ok:true,text:async()=>JSON.stringify(response)};
        },
        DOMParser:class {parseFromString(){return {querySelector:()=>next};}},
        FormData:class extends Map {constructor(form){super([['op', form.op]]);}},
        XMLHttpRequest:class {constructor(){this.upload = {}; xhr = this;} open(){} setRequestHeader(){} send(){}},
        history:{replaceState(){}}
    });
    const flush = () => new Promise(resolve => setImmediate(resolve));
    async function tick() {
        await flush();
        const [id, timer] = [...pending][0];
        pending.delete(id);
        await timer.cb();
        await flush();
    }
    return {parts, monitor, document, current, requests, pending, flush, tick, xhr:()=>xhr};
}

const running = {busy:true, job:'j', title:'Recebendo o pacote', events:[[1,'Recebendo o pacote']], transfer:[25,100]};
const done = {...running, busy:false, title:'Operação concluída', transfer:null, hold:false, attention:false};

test('polls while collapsed, measures bytes and recovers after the web service returns', async () => {
    const f = fixture([running, running, new Error('offline'), done, {fragment:true}]);
    await f.flush();
    assert.equal(f.parts.details.open, false);
    f.parts.ol.scrollHeight = 180;
    f.parts.details.open = true;
    f.parts.details.events.toggle();
    assert.equal(f.parts.ol.scrollTop, 180);
    f.parts.details.open = false;
    assert.equal(f.parts.progress.value,25);
    assert.equal(f.parts['.gu-meter-text'].textContent,'25% do pacote');
    const firstRow = f.parts.ol.children[0];
    await f.tick();
    assert.equal(f.parts.ol.children[0],firstRow);
    assert.equal(f.parts['.gu-connection'].hidden,true);
    await f.tick();
    assert.equal(f.parts['.gu-connection'].hidden,false);
    await f.tick();
    assert.equal(f.parts.strong.textContent,done.title);
    assert.equal(f.parts.ol.children.length,1);
    assert.equal(f.parts.details.open,false);
    assert.equal(f.current.replaced,true);
    assert.equal(f.monitor.hidden,true);
    assert.equal(f.pending.size,0);
    assert.match(f.requests.at(-1), /aba=backups&conteudo=1/);
});

test('reports when another form submits during an active operation', async () => {
    const f = fixture([running]);
    await f.flush();
    f.document.events.submit({target:{method:'post',op:'btest',closest:()=>f.current}, preventDefault(){}});
    assert.equal(f.parts['.gu-request-feedback'].hidden,false);
    assert.match(f.parts['.gu-request-feedback'].textContent,/Já há uma operação/);
    assert.equal(f.xhr(),undefined);
});

test('shows upload progress before the executor replies and follows with read-only polling', async () => {
    const f = fixture([done,{fragment:true}],false);
    const form = {method:'post',op:'upload',closest:()=>f.current};
    f.document.events.submit({target:form,preventDefault(){}});
    assert.equal(f.current.inert,true);
    assert.equal(f.monitor.hidden,false);
    const xhr = f.xhr();
    xhr.upload.onprogress({lengthComputable:true,total:10,loaded:3});
    assert.equal(f.parts['.gu-meter-text'].textContent,'30% enviado');
    xhr.upload.onload();
    assert.equal(f.parts.progress.value,undefined);
    xhr.status = 0;
    xhr.onloadend();
    await f.flush();
    assert.equal(f.current.replaced,true);
    assert.equal(f.parts['.gu-request-feedback'].hidden,false);
    assert.equal(f.requests.length,2);
});
