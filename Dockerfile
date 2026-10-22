# syntax=docker/dockerfile:1
# check=skip=UndefinedVar # We set the variables as a reference

ARG PYTHON_VERSION=3.12-slim
ARG OS_VARIANT=bookworm
ARG ODOO_VERSION
ARG WKHTMLTOX_VERSION=0.12.6.1-3
ARG ODOO_USER=odoo
ARG ODOO_BASEPATH=/opt/odoo
ARG APP_UID=1000
ARG APP_GID=1000

FROM python:${PYTHON_VERSION}-${OS_VARIANT} AS base

SHELL ["/bin/bash", "-xo", "pipefail", "-c"]

USER root

# Library versions
ARG WKHTMLTOX_VERSION
ENV WKHTMLTOX_VERSION=${WKHTMLTOX_VERSION}

# Use noninteractive to get rid of apt-utils message
ENV DEBIAN_FRONTEND=noninteractive

# Runtime packages only. Odoo's Python packages come from pip, so no Debian python3-* packages;
# libcairo2, libldap, libsasl2, and libmagic1 are the shared libraries those packages load.
# hadolint ignore=DL3008
RUN . /etc/os-release \
    && apt-get -qq update \
    && apt-get -qq install -y --no-install-recommends \
    ca-certificates \
    curl \
    fonts-liberation2 \
    fonts-noto-cjk \
    libcairo2 \
    libldap-2.5-0 \
    libmagic1 \
    libsasl2-2 \
    node-less \
    nodejs \
    unzip \
    xz-utils \
    zip \
    && curl -o wkhtmltox.deb -sSL "https://github.com/wkhtmltopdf/packaging/releases/download/${WKHTMLTOX_VERSION}/wkhtmltox_${WKHTMLTOX_VERSION}.${VERSION_CODENAME}_$(dpkg --print-architecture).deb" \
    && apt-get install -y --no-install-recommends ./wkhtmltox.deb \
    && apt-get autopurge -yqq \
    && rm -rf /var/lib/apt/lists/* wkhtmltox.deb /tmp/*

# install latest postgresql-client
RUN . /etc/os-release \
    && apt-get -qq update \
    && apt-get -qq install -y --no-install-recommends dirmngr gnupg \
    && echo "deb http://apt.postgresql.org/pub/repos/apt/ ${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && GNUPGHOME="$(mktemp -d)" \
    && export GNUPGHOME \
    && repokey='B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8' \
    && gpg --batch --keyserver keyserver.ubuntu.com --recv-keys "${repokey}" \
    && gpg --batch --armor --export "${repokey}" > /etc/apt/trusted.gpg.d/pgdg.gpg.asc \
    && gpgconf --kill all \
    && rm -rf "$GNUPGHOME" \
    && apt-get -qq update \
    && apt-get -qq install -y --no-install-recommends postgresql-client \
    && rm -f /etc/apt/sources.list.d/pgdg.list \
    && apt-get purge -yqq --auto-remove dirmngr gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install rtlcss; npm is only needed for this step.
RUN apt-get -qq update \
    && apt-get -qq install -y --no-install-recommends npm \
    && npm install -g rtlcss \
    && apt-get purge -yqq --auto-remove npm \
    && rm -Rf ~/.npm /var/lib/apt/lists/* /tmp/*

FROM base AS builder

# Install hard & soft build dependencies
RUN . /etc/os-release \
    && echo "deb http://apt.postgresql.org/pub/repos/apt/ ${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
    apt-utils dialog \
    apt-transport-https \
    build-essential \
    git \
    libcairo2-dev \
    libfreetype6-dev \
    libfribidi-dev \
    libghc-zlib-dev \
    libharfbuzz-dev \
    libjpeg-dev \
    libgeoip-dev \
    libmaxminddb-dev \
    liblcms2-dev \
    libldap2-dev \
    libopenjp2-7-dev \
    libpq-dev \
    libsasl2-dev \
    libssl-dev \
    libtiff5-dev \
    libxml2-dev \
    libxslt1-dev \
    libwebp-dev \
    tcl-dev \
    tk-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/* /tmp/*

# Install Odoo source code and install it as a package inside the container with additional tools
ARG ODOO_VERSION
ARG ODOO_REF=${ODOO_VERSION}

RUN git init /opt/odoo \
    && git -C /opt/odoo remote add origin https://github.com/odoo/odoo.git \
    && git -C /opt/odoo fetch --depth=1 origin ${ODOO_REF} \
    && git -C /opt/odoo checkout --detach FETCH_HEAD

# Install into /install, so the image copies only what pip adds and not the Python it already has.
# The pinned packages are what Odoo's apps import beyond requirements.txt, at the versions tested with them.
# The second pip cannot see what the first installed, and google-auth and pdfminer.six would pull a cryptography
# that pyOpenSSL 24.1.0 cannot load, so Odoo's pins constrain it.
RUN pip3 install --prefix=/install --no-cache-dir --upgrade --requirement /opt/odoo/requirements.txt \
    && pip3 -qq install --prefix=/install --no-cache-dir --upgrade --constraint /opt/odoo/requirements.txt \
    rlpycairo \
    'websocket-client~=0.56' \
    google-auth==2.62.0 \
    odfpy==1.4.1 \
    pdfminer.six==20260107 \
    PyJWT==2.15.1 \
    packaging \
    phonenumbers \
    markdown2 \
    && rm -rf /var/lib/apt/lists/* /tmp/*

# PYTHONPATH lets pip see what /install already holds, so it adds only what setup.py needs beyond requirements.txt (pypdf).
RUN PYTHONPATH="$(python3 -c "import sysconfig; print(sysconfig.get_path('purelib', vars={'base': '/install', 'platbase': '/install'}))")" \
    pip3 install --prefix=/install --no-cache-dir --editable /opt/odoo --config-settings editable_mode=compat \
    && rm -rf /opt/odoo/.git /var/lib/apt/lists/* /tmp/*

FROM base AS runtime

ARG ODOO_USER
ARG APP_UID
ARG APP_GID
ARG ODOO_BASEPATH

RUN addgroup --system --gid ${APP_GID} ${ODOO_USER} \
    && adduser --system --uid ${APP_UID} --ingroup ${ODOO_USER} --home ${ODOO_BASEPATH} --disabled-login --shell /bin/bash ${ODOO_USER}

# Odoo 20 reads ODOO_<OPTION> and PG* variables itself (see odoo/tools/config.py); these are the image's defaults.
# Odoo listens on 127.0.0.1 unless told otherwise, which a container's port mapping cannot reach.
ENV APP_UID=${APP_UID} \
    APP_GID=${APP_GID} \
    ODOO_BASEPATH=${ODOO_BASEPATH} \
    ODOO_EXTRA_ADDONS=/mnt/extra-addons \
    ODOO_RC=/etc/odoo/odoo.conf \
    ODOO_DATA_DIR=/var/lib/odoo/data \
    ODOO_LIST_DB=False \
    ODOO_HTTP_INTERFACE=0.0.0.0

RUN mkdir -p ${ODOO_DATA_DIR} ${ODOO_EXTRA_ADDONS} /etc/odoo \
    && chown ${APP_UID}:${APP_GID} ${ODOO_DATA_DIR} ${ODOO_EXTRA_ADDONS} /etc/odoo

VOLUME ["/var/lib/odoo/data"]

COPY --link --chown=${APP_UID}:${APP_GID} --from=builder /install /usr/local
COPY --link --chown=${APP_UID}:${APP_GID} --from=builder /opt/odoo ${ODOO_BASEPATH}

# dockerdoo_logging.json names this module, so Odoo imports it by name; it goes where Python looks for packages.
RUN --mount=type=bind,source=resources/dockerdoo_logging.py,target=/tmp/dockerdoo_logging.py \
    install -m 0644 /tmp/dockerdoo_logging.py "$(python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"

# Compiled once here, so each new container starts without compiling Odoo's code.
RUN python3 -m compileall -q -j 0 "$(python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))')" ${ODOO_BASEPATH}/odoo ${ODOO_BASEPATH}/addons

COPY --link --chmod=0755 ./resources/entrypoint.sh ./resources/getaddons.py ./resources/dockerdoo_init.py ./resources/containerlimits.py /
COPY --link --chmod=0644 ./resources/dockerdoo_logging.json /
# Add-ons to bake into the image, if any.
COPY --link --chown=${APP_UID}:${APP_GID} ./custom ${ODOO_EXTRA_ADDONS}

EXPOSE 8069 8072

HEALTHCHECK CMD curl --fail --silent "http://127.0.0.1:${ODOO_HTTP_PORT:-${PORT:-8069}}/web/health" || exit 1

# Starts as root so it can take ownership of a root-owned volume; the entrypoint then drops to the odoo user.
ENTRYPOINT ["/entrypoint.sh"]

CMD ["odoo"]

# The runtime plus what working on add-ons inside the container needs: compilers and headers for add-on
# requirements, git, npm, ffmpeg for browser-test screencasts, watchdog for --dev=reload, and debugpy.
FROM runtime AS development

ARG ODOO_USER

# libpq-dev must match the libpq of the PGDG postgresql-client in base, as in the builder.
RUN . /etc/os-release \
    && echo "deb http://apt.postgresql.org/pub/repos/apt/ ${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get -qq update \
    && apt-get -qq install -y --no-install-recommends \
    build-essential \
    ffmpeg \
    git \
    htop \
    less \
    libffi-dev \
    libjpeg62-turbo-dev \
    libldap2-dev \
    libpq-dev \
    libsasl2-dev \
    libssl-dev \
    libxml2-dev \
    libxslt1-dev \
    nano \
    npm \
    openssh-client \
    sudo \
    zlib1g-dev \
    && echo "${ODOO_USER} ALL=(root) NOPASSWD:ALL" > /etc/sudoers.d/${ODOO_USER} \
    && chmod 0440 /etc/sudoers.d/${ODOO_USER} \
    && pip3 install --no-cache-dir debugpy watchdog \
    && rm -f /etc/apt/sources.list.d/pgdg.list \
    && rm -rf /var/lib/apt/lists/* /tmp/*

# Last, so a build without --target produces the production image.
FROM runtime AS production
