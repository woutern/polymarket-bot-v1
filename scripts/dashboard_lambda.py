"""Lambda handler for dashboard — wraps FastAPI via Mangum."""

from mangum import Mangum

# Import the existing FastAPI app
from dashboard import app

handler = Mangum(app, lifespan="off")
