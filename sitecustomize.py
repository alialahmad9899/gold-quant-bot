"""Load and activate the repository's Twelve Data request guard before bot startup."""

import requests
import twelve_data_gateway

if not getattr(requests, "_gold_quant_twelve_data_guard_installed", False):
    requests.get = twelve_data_gateway.wrap_requests_get(requests.get)
    requests._gold_quant_twelve_data_guard_installed = True
