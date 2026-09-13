from pathlib import Path


ROOT = Path(__file__).parents[2]
API_GATEWAY = (ROOT / "terraform" / "api_gateway.tf").read_text()
COGNITO = (ROOT / "terraform" / "cognito.tf").read_text()


def test_all_dynamic_file_routes_require_the_jwt_authorizer():
    assert 'authorization_type   = "JWT"' in API_GATEWAY
    assert "authorizer_id" in API_GATEWAY
    assert 'authorization_scopes = ["openid"]' in API_GATEWAY
    assert "for_each = local.routes" in API_GATEWAY


def test_cors_allows_bearer_token_header():
    assert 'allow_headers = ["authorization", "content-type"]' in API_GATEWAY


def test_shared_throttle_is_small_and_explicit():
    assert "throttling_rate_limit  = 5" in API_GATEWAY
    assert "throttling_burst_limit = 10" in API_GATEWAY


def test_spa_client_has_no_secret_and_uses_code_flow():
    assert "generate_secret" in COGNITO
    assert "generate_secret                      = false" in COGNITO
    assert 'allowed_oauth_flows                  = ["code"]' in COGNITO
