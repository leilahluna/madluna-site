// Runs in front of every madluna.ca/w/* request.
//  - /w/<slug>/...      needs that household's password (from their card) before any page or photo is sent
//  - /w/<slug>/upload   lets a logged-in guest send us a photo or video; it lands in the private R2 bucket.
//                       Big files (dance videos!) come in 5 MB pieces via /upload/start, /part, /complete.
//  - /w/_admin/...      lists and fetches guest uploads for "python build.py uploads" (needs the admin token)
// generated/config.js is written by "python build.py build" and never goes to GitHub.
import config from './generated/config.js';

const enc = new TextEncoder();
const MAX_UPLOAD = 95 * 1024 * 1024;   // Cloudflare's free plan accepts requests up to 100 MB
const MAX_FILE = 4 * 1024 * 1024 * 1024;   // biggest single file, sent in pieces (a long 4K video fits)
const YEAR = 60 * 60 * 24 * 365;
const COMMON = {
	'X-Robots-Tag': 'noindex, nofollow, noimageindex',
	'Referrer-Policy': 'no-referrer',
	'X-Content-Type-Options': 'nosniff',
};

let keyPromise;
function hmacKey() {
	keyPromise ??= crypto.subtle.importKey('raw', enc.encode(config.secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
	return keyPromise;
}
function hex(buf) {
	return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
}
async function hmacHex(msg) {
	return hex(await crypto.subtle.sign('HMAC', await hmacKey(), enc.encode(msg)));
}
function same(a, b) {
	const x = enc.encode(a), y = enc.encode(b);
	return x.byteLength === y.byteLength && crypto.subtle.timingSafeEqual(x, y);
}
// "Maple 482", "maple-482" and "maple482" all count as the same password
function normalize(pw) {
	return pw.toLowerCase().replace(/[^a-z0-9]/g, '');
}

function withHeaders(res, extra = {}) {
	const out = new Response(res.body, res);
	for (const [k, v] of Object.entries({ ...COMMON, ...extra })) out.headers.set(k, v);
	return out;
}
function json(data, status = 200) {
	return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...COMMON } });
}
function cookieName(slug) {
	return 'w_' + slug.replace(/-/g, '_');
}
function getCookie(req, name) {
	for (const part of (req.headers.get('Cookie') || '').split(';')) {
		const [k, ...v] = part.trim().split('=');
		if (k === name) return v.join('=');
	}
	return '';
}

async function notFound(env, req) {
	// any missing asset path gets the 404 page (not_found_handling: 404-page)
	const res = await env.ASSETS.fetch(new Request(new URL('/w/__missing__', req.url)));
	return withHeaders(res, { 'Cache-Control': 'no-store' });
}

function loginPage(error = '', status = 200) {
	const body = config.loginHtml.replace('<!--ERROR-->', error ? `<p class="error" role="alert">${error}</p>` : '');
	return new Response(body, { status, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store', ...COMMON } });
}

async function login(req, slug, house, token) {
	const form = await req.formData().catch(() => null);
	const pw = normalize(String(form?.get('password') || ''));
	const hash = hex(await crypto.subtle.digest('SHA-256', enc.encode(`${config.secret}:${slug}:${pw}`)));
	if (!pw || !same(hash, house.hash)) {
		await new Promise((r) => setTimeout(r, 800));   // slow down guessing a little
		return loginPage("That password doesn't match. Check your card and try again.", 401);
	}
	return new Response(null, {
		status: 303,
		headers: {
			Location: `/w/${slug}/`,
			'Set-Cookie': `${cookieName(slug)}=${token}; Path=/w/${slug}/; Max-Age=${YEAR}; HttpOnly; Secure; SameSite=Lax`,
			'Cache-Control': 'no-store',
			...COMMON,
		},
	});
}

function mediaType(raw) {
	const type = String(raw || '').split(';')[0].trim().toLowerCase();
	return /^(image|video)\//.test(type) ? type : '';
}
function newKey(slug, rawName) {
	const name = String(rawName || 'upload').replace(/[^\w.\- ]+/g, '_').slice(-80) || 'upload';
	const stamp = new Date().toISOString().replace(/[:.]/g, '-');
	return { name, key: `inbox/${slug}/${stamp}-${crypto.randomUUID().slice(0, 8)}-${name}` };
}

// small files: one request
async function upload(req, env, slug, house) {
	const len = Number(req.headers.get('Content-Length') || 0);
	const type = mediaType(req.headers.get('Content-Type'));
	if (!len || !req.body) return json({ error: 'empty' }, 400);
	if (len > MAX_UPLOAD) return json({ error: 'too big' }, 413);
	if (!type) return json({ error: 'only photos and videos' }, 415);
	let raw = 'upload';
	try {
		raw = decodeURIComponent(req.headers.get('X-File-Name') || 'upload');
	} catch {}
	const { name, key } = newKey(slug, raw);
	// hand the request body straight to R2: piping it through JS would burn the free plan's CPU time on big files
	await env.UPLOADS.put(key, req.body, {
		httpMetadata: { contentType: type },
		customMetadata: { household: house.name, original: name },
	});
	return json({ ok: true });
}

// big files: start, then numbered pieces, then complete. R2 joins the pieces back into the original file.
async function uploadInPieces(req, env, url, slug, house, step) {
	if (step === 'start') {
		const info = await req.json().catch(() => ({}));
		const type = mediaType(info.type);
		if (!type) return json({ error: 'only photos and videos' }, 415);
		if (!(info.size > 0) || info.size > MAX_FILE) return json({ error: 'too big' }, 413);
		const { name, key } = newKey(slug, info.name);
		const mp = await env.UPLOADS.createMultipartUpload(key, {
			httpMetadata: { contentType: type },
			customMetadata: { household: house.name, original: name },
		});
		return json({ key: mp.key, uploadId: mp.uploadId });
	}
	// every later step names an upload that must belong to this household
	const q = step === 'part' ? Object.fromEntries(url.searchParams) : await req.json().catch(() => ({}));
	if (!String(q.key || '').startsWith(`inbox/${slug}/`) || !q.uploadId) return json({ error: 'bad upload' }, 400);
	const mp = env.UPLOADS.resumeMultipartUpload(q.key, q.uploadId);
	if (step === 'part') {
		const n = Number(q.n);
		const len = Number(req.headers.get('Content-Length') || 0);
		if (!(n >= 1 && n <= 10000) || !len || len > MAX_UPLOAD || !req.body) return json({ error: 'bad piece' }, 400);
		const part = await mp.uploadPart(n, req.body);
		return json({ partNumber: part.partNumber, etag: part.etag });
	}
	if (step === 'complete') {
		await mp.complete(q.parts || []);
		return json({ ok: true });
	}
	if (step === 'abort') {
		await mp.abort().catch(() => {});
		return json({ ok: true });
	}
	return json({ error: 'unknown step' }, 404);
}

async function admin(req, env, url, rest) {
	if (!same(req.headers.get('Authorization') || '', 'Bearer ' + config.adminToken)) {
		return new Response('Unauthorized', { status: 401, headers: COMMON });
	}
	if (rest === '/uploads') {
		const out = [];
		let cursor;
		do {
			const page = await env.UPLOADS.list({ prefix: 'inbox/', cursor, include: ['customMetadata'] });
			for (const o of page.objects) {
				out.push({ key: o.key, size: o.size, uploaded: o.uploaded, household: o.customMetadata?.household || '' });
			}
			cursor = page.truncated ? page.cursor : undefined;
		} while (cursor);
		return json(out);
	}
	if (rest === '/file') {
		const key = url.searchParams.get('key') || '';
		if (!key.startsWith('inbox/')) return new Response('Not found', { status: 404, headers: COMMON });
		if (req.method === 'DELETE') {
			await env.UPLOADS.delete(key);
			return json({ ok: true });
		}
		const obj = await env.UPLOADS.get(key);
		if (!obj) return new Response('Not found', { status: 404, headers: COMMON });
		return new Response(obj.body, { headers: { 'Content-Type': obj.httpMetadata?.contentType || 'application/octet-stream', ...COMMON } });
	}
	return new Response('Not found', { status: 404, headers: COMMON });
}

export default {
	async fetch(req, env) {
		const url = new URL(req.url);
		const m = url.pathname.match(/^\/w\/([a-z0-9_-]+)(\/.*)?$/);
		if (!m) return notFound(env, req);
		const [, slug, rest = ''] = m;
		if (slug === '_admin') return admin(req, env, url, rest);

		const house = config.households[slug];
		if (!house) return notFound(env, req);
		if (rest === '') return Response.redirect(`${url.origin}/w/${slug}/`, 301);

		const token = await hmacHex('auth:' + slug);
		if (rest === '/' && req.method === 'POST') return login(req, slug, house, token);
		if (!same(getCookie(req, cookieName(slug)), token)) {
			return rest === '/' ? loginPage() : new Response('Unauthorized', { status: 401, headers: COMMON });
		}

		if (rest === '/upload' && req.method === 'POST') return upload(req, env, slug, house);
		const piece = rest.match(/^\/upload\/(start|part|complete|abort)$/);
		if (piece && (req.method === 'POST' || req.method === 'PUT')) return uploadInPieces(req, env, url, slug, house, piece[1]);
		if (req.method !== 'GET' && req.method !== 'HEAD') return new Response('Method not allowed', { status: 405, headers: COMMON });
		const res = await env.ASSETS.fetch(req);
		const media = rest.startsWith('/thumbs/') || rest.startsWith('/view/') || rest.startsWith('/full/');
		return withHeaders(res, { 'Cache-Control': media ? 'private, max-age=86400' : 'private, no-cache' });
	},
};
