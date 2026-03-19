from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


def custom_openapi(app: FastAPI):
    """Custom OpenAPI schema with JWT authentication"""
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    # Add authentication schemes
    openapi_schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "JWT token for professors and super admins. Get token from /api/v2/auth/login",
        },
        "ModuleToken": {
            "type": "apiKey",
            "in": "query",
            "name": "module_token",
            "description": "64-character module access token for widget authentication. No user login required.",
        },
    }

    # Apply security schemes based on route patterns
    for path_key, path_value in openapi_schema["paths"].items():
        # Widget routes use module token authentication
        if path_key.startswith("/api/widget"):
            for method_key, method_value in path_value.items():
                method_value["security"] = [{"ModuleToken": []}]
        # Management routes use JWT authentication
        elif not path_key.startswith("/api/v2/auth") and path_key not in [
            "/",
            "/health",
        ]:
            for method_key, method_value in path_value.items():
                method_value["security"] = [{"BearerAuth": []}]

    app.openapi_schema = openapi_schema
    return app.openapi_schema
