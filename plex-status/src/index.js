export default {
	async fetch(request) {
		const corsHeaders = {
			'Access-Control-Allow-Origin': 'https://madluna.ca',
			'Access-Control-Allow-Methods': 'GET',
			'Content-Type': 'application/json',
		};

		if (request.method === 'OPTIONS') {
			return new Response(null, { headers: corsHeaders });
		}

		try {
			const res = await fetch('http://plex.madluna.ca:32400/identity', {
				signal: AbortSignal.timeout(5000),
			});
			const online = res.ok;
			return new Response(
				JSON.stringify({ online }),
				{ headers: corsHeaders }
			);
		} catch {
			return new Response(
				JSON.stringify({ online: false }),
				{ headers: corsHeaders }
			);
		}
	},
};
