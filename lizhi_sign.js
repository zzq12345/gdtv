/*
 * lizhi_sign.js —— 荔枝网 (gdtv.cn) 请求签名
 * ====================================================
 * 修复要点：
 * 1. __wbindgen_throw 直接使用 importObj.env.memory，避免依赖 wasmExports
 * 2. 保留通用调用框架，实际签名函数名需根据 wasm 导出表调整（见注释）
 * 3. 增加更健壮的异常捕获
 */

const fs = require("fs");
const path = require("path");

// ── 配置 ──
const DEVICE_ID = "WEB_f7461cd0-fde8-11ec-b3cc-c120738f1955";
const CLIENT    = "WEB_PC";
const WASM_PATH = path.join(__dirname, "lizhi.wasm");

let wasmExports = null;

// ── WASM 初始化 ──
async function initWasm() {
    if (wasmExports) return wasmExports;

    if (!fs.existsSync(WASM_PATH)) {
        throw new Error(
            `[!] 未找到 lizhi.wasm，请从 gdtv.cn 页面提取并保存到 ${WASM_PATH}`
        );
    }

    const buf = fs.readFileSync(WASM_PATH);
    const memory = new WebAssembly.Memory({ initial: 256 });

    // 直接使用 memory 构造错误回调，避免依赖外部变量
    const importObj = {
        env: {
            memoryBase: 0,
            tableBase: 0,
            memory: memory,
            table: new WebAssembly.Table({ initial: 0, element: "anyfunc" }),
            __wbindgen_throw: (ptr, len) => {
                const bytes = new Uint8Array(memory.buffer, ptr, len);
                const msg = Buffer.from(bytes).toString("utf8");
                throw new Error("[wasm] " + msg);
            },
            // 可能还需要其他环境函数，可按需添加
        },
    };

    const { instance } = await WebAssembly.instantiate(buf, importObj);
    wasmExports = instance.exports;
    return wasmExports;
}

// ── 签名主函数 ──
async function signRequest(method, url, deviceId, client, body) {
    const exports = await initWasm();

    // ★★★ 请根据实际 wasm 导出表调整函数名 ★★★
    // 常见名：encrypt / sign / gen_headers / hmac_sign
    const encryptFn = exports.encrypt
                   || exports.sign
                   || exports.gen_headers
                   || exports.hmac_sign;

    if (!encryptFn) {
        const available = Object.keys(exports).join(", ");
        throw new Error(`[!] wasm 中未找到签名函数，可用导出: ${available}`);
    }

    // 调用 wasm 函数，参数需根据实际接口调整（可能需传入指针）
    // 此处假设可以直接传递 JS 字符串（由 wasm 的 JS 胶水代码处理）
    const result = encryptFn(method, url, deviceId || DEVICE_ID, client || CLIENT, body || "");

    // 处理返回值（可能是对象、字符串、数字指针等）
    if (result && typeof result === "object" && !Array.isArray(result)) {
        return result;
    }
    if (typeof result === "string") {
        try { return JSON.parse(result); } catch { /* ignore */ }
    }
    // 若返回数字指针，需从内存读取数据（但此处不实现，留待用户按需扩展）
    return result;
}

// ── CLI 入口 ──
if (require.main === module) {
    (async () => {
        const [, , method, url, deviceId, client, body] = process.argv;

        if (!method || !url) {
            console.error("用法: node lizhi_sign.js <METHOD> <URL> [deviceId] [client] [body]");
            process.exit(1);
        }

        try {
            const headers = await signRequest(method, url, deviceId, client, body);
            // 输出 JSON 供 Python 端解析
            console.log(JSON.stringify(headers, null, 0));
        } catch (err) {
            console.error(err.message);
            process.exit(2);
        }
    })();
}

module.exports = { signRequest };
