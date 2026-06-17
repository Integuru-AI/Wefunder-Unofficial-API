# Wefunder Unofficial API

Unofficial Python integrations for Wefunder.

## Integrations

- `wefunder_get_offering_details.py` - `get_offering_details`.
- `wefunder_list_offerings.py` - `list_offerings`.
- `wefunder_fetch_offering_posts.py` - `fetch_offering_posts`.
- `wefunder_get_offering_media.py` - `get_offering_media`.

## Usage

Each file exposes a `run(input, context)` entrypoint. The runtime is expected to provide:

- `input`: integration-specific request fields.
- `context["headers"]`: authenticated request headers when required.
- `context["base_url"]`: the platform base URL when overriding the default.

Install dependencies:

```bash
pip install -r requirements.txt
```

## Info

This unofficial API is built by [Integuru](https://integuru.com).

For custom requests or hosted authentication, contact richard@integuru.com or [schedule time with us](https://calendly.com/d/cqb8-d9x-nbf/integuru).

See the [complete list of APIs by Integuru](https://github.com/Integuru-AI/APIs-by-Integuru).
