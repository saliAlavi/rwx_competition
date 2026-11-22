# Blast Radius entry. The base digest is the image every commit was labeled in:
# its Python and SQLite decide how sqlglot's tests behave, so we keep it.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    SKIP_INTEGRATION=0

# The exact packages the labels were produced with.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock

# sqlglot at the pinned commit, byte-compiled so forked test runs import fast.
COPY tools/fetch_sqlglot.py /app/tools/fetch_sqlglot.py
RUN python /app/tools/fetch_sqlglot.py /opt/sqlglot \
 && (python -m compileall -q -j 4 /opt/sqlglot/sqlglot /opt/sqlglot/tests || true) \
 && chmod -R a+rX /opt/sqlglot

COPY . /app
RUN python -m compileall -q /app/br /app/main.py \
 && python /app/tools/prepare.py \
 && chmod -R a+rX /app

CMD ["python", "-X", "frozen_modules=on", "/app/main.py"]
