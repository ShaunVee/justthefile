# Reddit relay

Reddit blocks this project's VPS by IP address. Measured from the server on
2026-07-24, with a browser User-Agent:

| request | result |
| --- | --- |
| `www.reddit.com/r/…/s/<token>` | 403, page titled "Blocked" |
| `old.reddit.com/comments/<id>/` | 403 |
| `www.reddit.com/comments/<id>/.json` | 403 |
| `api.reddit.com/comments/<id>` | 403 |
| `oauth.reddit.com/comments/<id>` | 403 |
| `v.redd.it/<id>/CMAF_720.mp4` | 206 |
| `v.redd.it/<id>/DASHPlaylist.mpd` | 200 |
| `i.redd.it/<anything>` | 404, meaning reachable |

The ban covers the website, not the CDNs. Video, audio and images keep coming
straight from the server at full speed. Only the lookups: two provider pages
and the `/s/` share-link hop, need an address Reddit will answer.

Reddit's own API was the supported way through that and is closed. Self-serve
app creation ended in late 2025; new credentials require an approved
application under the Responsible Builder Policy. Hence a relay.

`worker.js` is that relay: a Cloudflare Worker that passes those three
requests along and nothing else. It is locked to `reddit.com` and its
subdomains and requires a shared secret, so it cannot be found and used as an
open proxy. Traffic is a few hundred KB per link, well inside the free plan's
100,000 requests a day.

## Deploying it

1. At <https://dash.cloudflare.com>, go to **Workers & Pages** and create a
   worker. Any name; `jtf-relay` is the one this project uses.
2. **Edit code**, replace the contents with `worker.js`, **Deploy**.
3. **Settings → Variables and Secrets → Add**, type **Secret**, name
   `RELAY_KEY`, value from `openssl rand -hex 24`.
4. Put the worker's address and that same secret in `.env` as
   `REDDIT_RELAY_URL` and `REDDIT_RELAY_KEY`, then `docker compose up -d`.

Check it end to end from the server, which is the only place the answer means
anything:

```sh
curl -s -H "X-Relay-Key: $REDDIT_RELAY_KEY" \
  "$REDDIT_RELAY_URL?mode=redirect&url=https://www.reddit.com/r/MemeVideos/s/tt30Dn0FHu"
```

A JSON body with a `final` field is a working relay. `{"status": 403, "final":
null}` means Cloudflare's addresses are blocked too, and the relay cannot be
the answer: see the alternatives in `docs/DEPLOY.md`.

`unauthorized` and a 401 is the key, not Reddit: `REDDIT_RELAY_KEY` and the
worker's `RELAY_KEY` differ, or `.env` still holds the placeholder. The
services log `relay rejected our key` and name the variable, and neither of
them reports it as Reddit blocking the server, because the two look identical
from the outside and only one of them has a fix.

Set the variables in `.env` on the *host that runs the service*. Each service
reads its own copy, so a bot and a web front end on different machines, or one
container recreated and the other not, can disagree about whether the relay
exists at all: the symptom is one of them working perfectly while the other
insists Reddit is blocking it.

## When it is not set

Both variables blank means every Reddit request goes out directly, exactly as
before. That is what a laptop wants, what the test suite assumes, and what any
host Reddit hasn't blocked should use: one less moving part, one less thing to
expire.
