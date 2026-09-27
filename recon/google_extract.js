() => {
    const visible = el => !!el && el.getClientRects().length > 0;
    const text = el => el ? el.innerText.trim() : '';
    const displayedDate = /^(\d{1,2} [A-Z][a-z]{2,4}\.? \d{4}|[A-Z][a-z]{2,4}\.? \d{1,2}, \d{4}|\d+ (minute|hour|day|week|month|year)s? ago)$/;
    const body = text(document.body);
    if (location.pathname.startsWith('/sorry') ||
        document.querySelector('form[action*="/sorry"], #captcha-form') ||
        (/unusual traffic from your computer network/i.test(body) &&
         !document.querySelector('#search h3'))) {
        return {state: 'captcha', results: []};
    }
    if (location.hostname.startsWith('consent.google.') ||
        (/Before you continue to Google/i.test(body) &&
         !document.querySelector('#search h3'))) {
        return {state: 'consent', results: []};
    }
    const root = document.querySelector('#rso') || document.querySelector('#search');
    const query = document.querySelector('textarea[name="q"], input[name="q"]')?.value ||
                  new URL(location.href).searchParams.get('q') || '';
    const top = text(document.querySelector('#topstuff'));
    const bottom = text(document.querySelector('#botstuff'));
    const empty = /did not match any documents|no results found for|there are no results for/i.test(top);
    const exhausted = empty || /in order to show you the most relevant results|we have omitted some entries/i.test(bottom);
    const results = [];
    const seenBlocks = new Set();
    let unparsed = 0;
    if (root) {
        for (const heading of root.querySelectorAll('h3')) {
            if (!visible(heading)) continue;
            if (heading.closest('#tads, #bottomads, [data-text-ad], [data-ad-slot]')) continue;
            const anchor = heading.closest('a[href]');
            if (!anchor) continue;
            let target;
            // Opaque /goto (or tokenised /url) links hide the destination; the
            // collector resolves them from the redirect's Location header.
            let redirect = null;
            try {
                target = new URL(anchor.href, location.href);
                if (target.origin === location.origin && ['/url', '/goto'].includes(target.pathname)) {
                    const wrapped = target.searchParams.get('q') || target.searchParams.get('url') || '';
                    if (/^https?:\/\//i.test(wrapped)) target = new URL(wrapped);
                    else redirect = target.href;
                }
            } catch { unparsed++; continue; }
            if (!['http:', 'https:'].includes(target.protocol)) continue;
            if (!redirect && (target.hostname === location.hostname || target.hostname === 'accounts.google.com')) continue;
            const block = heading.closest('.MjjYud, .g, .tF2Cxc, [data-sokoban-container]');
            if (!block) { unparsed++; continue; }
            // A nested sitelink is not another ranked organic result.
            if (seenBlocks.has(block)) continue;
            seenBlocks.add(block);
            const snippet = block.querySelector('.VwiC3b, .IsZvec, [data-sncf]');
            const date = block.querySelector('time, .MUxGbd.wuQ4Ob.WZ8Tjf');
            // Current layouts prefix the snippet with a displayed date ("23 Dec 2025 — ...");
            // the same span can hold other labels ("113 pages"), so only date-like text counts.
            const shown = [...block.querySelectorAll('.YrbPuc')].map(text).find(t => displayedDate.test(t));
            results.push({title: text(heading), url: redirect ? null : target.href, redirect,
                snippet: text(snippet), page_date: date?.getAttribute('datetime') || text(date) || shown || '',
                page_type: !redirect && /\.pdf$/i.test(target.pathname) ? 'pdf' : 'unknown'});
        }
    }
    const next = document.querySelector('a#pnnext, a[rel="next"]');
    return {state: empty ? 'empty' : root && results.length && !unparsed ? 'results' : 'unknown',
            query, results, next_url: visible(next) ? next.href : null,
            exhausted, unparsed_candidates: unparsed, ai_overview: aiOverview()};

    // The AI Overview is recorded separately from ranked results. Its full text and
    // source list are present before "Show more" is clicked; that button only expands
    // the display. Returns null when no overview is shown.
    function aiOverview() {
        const box = document.querySelector('[data-subtree="aimc"]');
        const heading = [...document.querySelectorAll('[role="heading"]')]
            .some(e => e.textContent.trim() === 'AI Overview');
        if (!box) return heading ? {text: '', sources: [], error: 'Overview shown but not recognised', complete: true} : null;
        // Google adds its closing disclaimer beside the overview only once generation has finished.
        const complete = !!box.parentElement && box.parentElement.textContent.includes('AI responses may include mistakes');
        const body = box.querySelector('.pWvJNd');
        const sources = [], seen = new Set();
        for (const a of box.querySelectorAll('a[aria-label$="Opens in a new tab."]')) {
            let link;
            try { link = new URL(a.getAttribute('href'), location.href); } catch { continue; }
            if (seen.has(link.href)) continue;
            seen.add(link.href);
            const google = link.origin === location.origin && ['/goto', '/url'].includes(link.pathname);
            const card = a.closest('li') || a.parentElement;
            const title = a.getAttribute('aria-label').replace(/\.? Opens in a new tab\.$/, '').trim();
            const site = [...card.querySelectorAll('span')].find(e => !e.children.length && e.textContent.trim());
            let excerpt = card.textContent.replace(/\s+/g, ' ').trim();
            for (const part of [site ? site.textContent.trim() : '', title]) {
                if (part && excerpt.startsWith(part)) excerpt = excerpt.slice(part.length).trim();
            }
            sources.push({position: sources.length + 1, site: site ? site.textContent.trim() : '', title,
                excerpt, url: google ? null : link.href, redirect: google ? link.href : null});
        }
        if (!body) return {text: '', sources, error: 'Overview text not recognised', complete};
        const lines = [];
        let line = '';
        const flush = () => { if (line.trim()) lines.push(line.replace(/\s+/g, ' ').trim()); line = ''; };
        const walk = el => {
            for (const node of el.childNodes) {
                if (node.nodeType === Node.TEXT_NODE) { line += node.textContent; continue; }
                if (node.nodeType !== Node.ELEMENT_NODE) continue;
                // Collapsed text is aria-hidden until "Show more", so aria-hidden is not skipped.
                if (['SCRIPT', 'STYLE', 'svg'].includes(node.tagName) || node.getAttribute('role') === 'dialog') continue;
                // Inline citation chips name the supporting site, e.g. [Gresham House].
                if (node.classList.contains('WBgIic')) {
                    const label = node.textContent.replace(/\s+/g, ' ').trim();
                    if (label) line += ` [${label}]`;
                    continue;
                }
                const block = /^(DIV|P|LI|UL|OL|H[1-6]|TABLE|TR)$/.test(node.tagName) || node.getAttribute('role') === 'heading';
                if (block) flush();
                if (node.tagName === 'LI') line += '- ';
                walk(node);
                if (block) flush();
            }
        };
        walk(body);
        flush();
        return {text: lines.join('\n'), sources, error: '', complete};
    }
}
