// Shared by a browser page and a dedicated Worker. Every run owns a fresh runtime.
export async function qualifyBrowser(progress) {
  const evidence={started:new Date().toISOString(),userAgent:navigator.userAgent,
    origin:location.origin,executionContext:typeof document==='undefined'?'worker':'page',
    crossOriginIsolated:globalThis.crossOriginIsolated};
  try {
    const manifest=await (await fetch('/manifest.json')).json();
    const indexURL=`https://cdn.jsdelivr.net/pyodide/v${manifest.pyodide}/full/`;
    progress('加载 Pyodide '+manifest.pyodide);
    const {loadPyodide}=await import(indexURL+'pyodide.mjs');
    const py=await loadPyodide({indexURL});evidence.runtimeVersion=py.version;
    await py.loadPackage('micropip');
    const wheelURL=new URL('/'+manifest.wheel,location.href).href;
    // Hash the downloaded wheel in the browser before micropip fetches the same
    // immutable local artifact; Python checks every installed module afterwards.
    const bytes=await (await fetch(wheelURL)).arrayBuffer();
    const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),n=>n.toString(16).padStart(2,'0')).join('');
    if(digest!==manifest.sha256)throw new Error('Wheel digest mismatch');
    evidence.downloadedWheelSha256=digest;
    progress('micropip 安装并核对 '+manifest.module_count+' 个模块');
    py.globals.set('browser_wheel_url',wheelURL);
    await py.runPythonAsync('import micropip\nawait micropip.install(browser_wheel_url, deps=False)');
    py.globals.set('browser_manifest_json',JSON.stringify(manifest));
    progress('执行模型读写、来源、迁移和原生能力拒绝检查');
    const code=await (await fetch('/probe.py')).text();
    evidence.probe=JSON.parse(await py.runPythonAsync(code));
    evidence.success=true;
  } catch(error) {evidence.success=false;evidence.error=String(error.stack||error);}
  evidence.finished=new Date().toISOString();return evidence;
}
