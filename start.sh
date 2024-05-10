while true; do
    screen -S open-workshop-storage-executor gunicorn main:app -b 0.0.0.0:8000 --access-logfile access.log --error-logfile error.log -c gunicorn_config.py --worker-class uvicorn.workers.UvicornWorker
    sleep 10
done