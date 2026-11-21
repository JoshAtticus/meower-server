# Load .env file
from dotenv import load_dotenv
load_dotenv()

import asyncio
import os
import uvicorn

from threading import Thread

from cloudlink import CloudlinkServer
from supporter import Supporter
from security import background_tasks_loop
from data_export import cleanup_loop as data_export_cleanup_loop
from rest_api import app as rest_api


if __name__ == "__main__":
    # Initialise Sentry, if configured
    if os.getenv("SENTRY_DSN"):
        import sentry_sdk
        from sentry_sdk.integrations.quart import QuartIntegration
        sentry_sdk.init(
            dsn=os.getenv("SENTRY_DSN"),
            integrations=[QuartIntegration()],
            environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", 0)),
        )

    # Create Cloudlink server
    cl = CloudlinkServer()

    # Create Supporter class
    supporter = Supporter(cl)
    cl.supporter = supporter

    # Start background tasks loop
    Thread(target=background_tasks_loop, daemon=True).start()

    # Delete expired data exports
    Thread(target=data_export_cleanup_loop, daemon=True).start()

    # Initialise REST API
    rest_api.cl = cl
    rest_api.supporter = supporter

    # Start REST API
    Thread(target=uvicorn.run, args=(rest_api,), kwargs={
        "host": os.getenv("API_HOST", "0.0.0.0"),
        "port": int(os.getenv("API_PORT", 3001)),
        "root_path": os.getenv("API_ROOT", "")
    }, daemon=True).start()

    # Start Cloudlink server
    asyncio.run(cl.run(host=os.getenv("CL3_HOST", "0.0.0.0"), port=int(os.getenv("CL3_PORT", 3000))))
