// Runs in front of every madluna.ca/w/* request.
//  - /w/<slug>/...      needs that household's password (from their card) before any page or photo is sent
//  - /w/<slug>/upload   lets a logged-in guest send us a photo or video; it lands in the private R2 bucket.
//                       Big files (dance videos!) come in 5 MB pieces via /upload/start, /part, /complete.
//  - /w/admin/          Merrick & Leilah's page for everything guests sent (its own password)
//  - /w/_admin/...      lists, fetches and marks guest uploads for "python build.py uploads" (needs the admin token)
// generated/config.js is written by "python build.py build" and never goes to GitHub.
import config from './generated/config.js';

const enc = new TextEncoder();
const MAX_UPLOAD = 95 * 1024 * 1024;   // Cloudflare's free plan accepts requests up to 100 MB
const MAX_FILE = 4 * 1024 * 1024 * 1024;   // biggest single file, sent in pieces (a long 4K video fits)
const YEAR = 60 * 60 * 24 * 365;
const STATE_KEY = 'admin/state.json';   // which uploads have been seen on the admin page / saved to the server
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
async function passwordHash(who, pw) {
	return hex(await crypto.subtle.digest('SHA-256', enc.encode(`${config.secret}:${who}:${normalize(pw)}`)));
}

function withHeaders(res, extra = {}) {
	const out = new Response(res.body, res);
	for (const [k, v] of Object.entries({ ...COMMON, ...extra })) out.headers.set(k, v);
	return out;
}
function json(data, status = 200) {
	return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...COMMON } });
}
function html(body, status = 200) {
	return new Response(body, { status, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store', ...COMMON } });
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

function loginPage(page, error = '', status = 200) {
	return html(page.replace('<!--ERROR-->', error ? `<p class="error" role="alert">${error}</p>` : ''), status);
}

// checks the posted password; on success sets a year-long cookie for that path and goes back to it
async function login(req, who, path, expectedHash, token, page) {
	const form = await req.formData().catch(() => null);
	const pw = String(form?.get('password') || '');
	if (!normalize(pw) || !same(await passwordHash(who, pw), expectedHash)) {
		await new Promise((r) => setTimeout(r, 800));   // slow down guessing a little
		const hint = who === 'admin' ? 'Try again.' : 'Check your card and try again.';
		return loginPage(page, `That password doesn't match. ${hint}`, 401);
	}
	return new Response(null, {
		status: 303,
		headers: {
			Location: path,
			'Set-Cookie': `${cookieName(who)}=${token}; Path=${path}; Max-Age=${YEAR}; HttpOnly; Secure; SameSite=Lax`,
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

// ===== guest uploads, for the admin page and the download command =====

async function readState(env) {
	const obj = await env.UPLOADS.get(STATE_KEY);
	const s = obj ? await obj.json().catch(() => ({})) : {};
	return { seen: s.seen || {}, saved: s.saved || {} };
}
async function updateState(env, change) {
	const s = await readState(env);
	change(s);
	await env.UPLOADS.put(STATE_KEY, JSON.stringify(s), { httpMetadata: { contentType: 'application/json' } });
}
async function listUploads(env) {
	const out = [];
	let cursor;
	do {
		const page = await env.UPLOADS.list({ prefix: 'inbox/', cursor, include: ['customMetadata', 'httpMetadata'] });
		for (const o of page.objects) {
			out.push({
				key: o.key,
				size: o.size,
				uploaded: o.uploaded,
				household: o.customMetadata?.household || '',
				slug: o.key.split('/')[1],
				name: o.customMetadata?.original || o.key.split('/').pop(),
				type: o.httpMetadata?.contentType || '',
			});
		}
		cursor = page.truncated ? page.cursor : undefined;
	} while (cursor);
	return out;
}
async function deleteUpload(env, key) {
	await env.UPLOADS.delete(key);
	await updateState(env, (s) => {
		delete s.seen[key];
		delete s.saved[key];
	});
}

// "bytes=0-1023", "bytes=500-" or "bytes=-500" -> an R2 range
function parseRange(header) {
	const m = /^bytes=(\d*)-(\d*)$/.exec(header || '');
	if (!m || (m[1] === '' && m[2] === '')) return null;
	if (m[1] === '') return { suffix: Number(m[2]) };
	const offset = Number(m[1]);
	return m[2] === '' ? { offset } : { offset, length: Number(m[2]) - offset + 1 };
}

// streams a stored upload; supports byte ranges so videos can play and skip around in the browser
async function serveUpload(env, req, key, download) {
	const range = parseRange(req.headers.get('Range'));
	const obj = await env.UPLOADS.get(key, range ? { range } : {});
	if (!obj) return new Response('Not found', { status: 404, headers: COMMON });
	const headers = new Headers(COMMON);
	obj.writeHttpMetadata(headers);
	headers.set('ETag', obj.httpEtag);
	headers.set('Accept-Ranges', 'bytes');
	headers.set('Cache-Control', 'private, max-age=3600');
	if (download) {
		const name = obj.customMetadata?.original || key.split('/').pop();
		headers.set('Content-Disposition', `attachment; filename="${name.replace(/[^\w.\- ]/g, '_')}"; filename*=UTF-8''${encodeURIComponent(name)}`);
	}
	let status = 200, length = obj.size;
	if (range) {
		const offset = 'suffix' in range ? Math.max(0, obj.size - range.suffix) : range.offset;
		length = Math.min(obj.size - offset, 'suffix' in range ? range.suffix : range.length ?? obj.size - offset);
		headers.set('Content-Range', `bytes ${offset}-${offset + length - 1}/${obj.size}`);
		status = 206;
	}
	headers.set('Content-Length', String(length));
	return new Response(req.method === 'HEAD' ? null : obj.body, { status, headers });
}

// Merrick & Leilah's admin page at /w/admin/
async function adminPage(req, env, url, rest) {
	const token = await hmacHex('auth:admin');
	if (rest === '') return Response.redirect(`${url.origin}/w/admin/`, 301);
	if (rest === '/' && req.method === 'POST') return login(req, 'admin', '/w/admin/', config.adminHash, token, config.adminLoginHtml);
	if (!same(getCookie(req, cookieName('admin')), token)) {
		return rest === '/' ? loginPage(config.adminLoginHtml) : new Response('Unauthorized', { status: 401, headers: COMMON });
	}
	if (rest === '/') return html(config.adminHtml);

	if (rest === '/api/items') {
		const [items, state] = await Promise.all([listUploads(env), readState(env)]);
		for (const it of items) {
			it.seen = !!state.seen[it.key];
			it.saved = !!state.saved[it.key];
		}
		const households = Object.entries(config.households).map(([slug, h]) => ({ slug, name: h.name }));
		return json({ items, households });
	}
	if (rest === '/api/seen' && req.method === 'POST') {
		const { keys = [] } = await req.json().catch(() => ({}));
		const now = new Date().toISOString();
		await updateState(env, (s) => keys.forEach((k) => (s.seen[k] ??= now)));
		return json({ ok: true });
	}
	if (rest === '/api/delete' && req.method === 'POST') {
		const { key = '' } = await req.json().catch(() => ({}));
		if (!key.startsWith('inbox/')) return json({ error: 'bad key' }, 400);
		await deleteUpload(env, key);
		return json({ ok: true });
	}
	if (rest === '/file') {
		const key = url.searchParams.get('key') || '';
		if (!key.startsWith('inbox/')) return new Response('Not found', { status: 404, headers: COMMON });
		return serveUpload(env, req, key, url.searchParams.has('download'));
	}
	return new Response('Not found', { status: 404, headers: COMMON });
}

// the download command ("python build.py uploads"), with the admin token instead of a password
async function adminApi(req, env, url, rest) {
	if (!same(req.headers.get('Authorization') || '', 'Bearer ' + config.adminToken)) {
		return new Response('Unauthorized', { status: 401, headers: COMMON });
	}
	if (rest === '/uploads') {
		const [items, state] = await Promise.all([listUploads(env), readState(env)]);
		return json(items.map((it) => ({ ...it, saved: !!state.saved[it.key] })));
	}
	if (rest === '/saved' && req.method === 'POST') {
		const { keys = [] } = await req.json().catch(() => ({}));
		const now = new Date().toISOString();
		await updateState(env, (s) => keys.forEach((k) => (s.saved[k] ??= now)));
		return json({ ok: true });
	}
	if (rest === '/file') {
		const key = url.searchParams.get('key') || '';
		if (!key.startsWith('inbox/')) return new Response('Not found', { status: 404, headers: COMMON });
		if (req.method === 'DELETE') {
			await deleteUpload(env, key);
			return json({ ok: true });
		}
		return serveUpload(env, req, key, false);
	}
	return new Response('Not found', { status: 404, headers: COMMON });
}

export default {
	async fetch(req, env) {
		const url = new URL(req.url);
		const m = url.pathname.match(/^\/w\/([a-z0-9_-]+)(\/.*)?$/);
		if (!m) return notFound(env, req);
		const [, slug, rest = ''] = m;
		if (slug === '_admin') return adminApi(req, env, url, rest);
		if (slug === 'admin') return adminPage(req, env, url, rest);

		const house = config.households[slug];
		if (!house) return notFound(env, req);
		if (rest === '') return Response.redirect(`${url.origin}/w/${slug}/`, 301);

		const token = await hmacHex('auth:' + slug);
		if (rest === '/' && req.method === 'POST') return login(req, slug, `/w/${slug}/`, house.hash, token, config.loginHtml);
		if (!same(getCookie(req, cookieName(slug)), token)) {
			return rest === '/' ? loginPage(config.loginHtml) : new Response('Unauthorized', { status: 401, headers: COMMON });
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
