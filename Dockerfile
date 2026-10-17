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

# Install odoo deps
# hadolint ignore=DL3008
RUN apt-get -qq update \
    && apt-get -qq install -y --no-install-recommends \
    # Odoo dependencies
    ca-certificates \
    curl \
    dirmngr \
    fonts-noto-cjk \
    gnupg \
    libmagic1 \
    libssl-dev \
    node-less \
    npm \
    # This uses a buggy version of libmagic
    # python3-magic \
    python3-num2words \
    python3-odf \
    python3-pdfminer \
    python3-pip \
    python3-phonenumbers \
    python3-pyldap \
    python3-qrcode \
    python3-renderpm \
    python3-setuptools \
    python3-slugify \
    python3-vobject \
    python3-watchdog \
    python3-xlrd \
    python3-xlwt \
    # Other dependencies
    git-core \
    ffmpeg \
    fonts-liberation2 \
    lsb-release \
    unzip \
    zip \
    xz-utils \
    && \
    if [ "$(uname -m)" = "aarch64" ]; then \
        curl -o wkhtmltox.deb -sSL https://github.com/wkhtmltopdf/packaging/releases/download/${WKHTMLTOX_VERSION}/wkhtmltox_${WKHTMLTOX_VERSION}.$(lsb_release -cs)_arm64.deb \
    ; else \
        curl -o wkhtmltox.deb -sSL https://github.com/wkhtmltopdf/packaging/releases/download/${WKHTMLTOX_VERSION}/wkhtmltox_${WKHTMLTOX_VERSION}.$(lsb_release -cs)_amd64.deb \
    ; fi \
    && apt-get install -y --no-install-recommends ./wkhtmltox.deb \
    && apt-get autopurge -yqq \
    && rm -rf /var/lib/apt/lists/* wkhtmltox.deb /tmp/*

# install latest postgresql-client
RUN apt-get -qq update \
    && apt-get -qq install -y --no-install-recommends \
    lsb-release \
    && echo "deb http://apt.postgresql.org/pub/repos/apt/ $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && GNUPGHOME="$(mktemp -d)" \
    && export GNUPGHOME \
    && repokey='B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8' \
    && gpg --batch --keyserver keyserver.ubuntu.com --recv-keys "${repokey}" \
    && gpg --batch --armor --export "${repokey}" > /etc/apt/trusted.gpg.d/pgdg.gpg.asc \
    && gpgconf --kill all \
    && rm -rf "$GNUPGHOME" \
    && apt-get -qq install -y --no-install-recommends postgresql-client libpq-dev \
    && rm -f /etc/apt/sources.list.d/pgdg.list \
    && rm -rf /var/lib/apt/lists/*

# Install rtlcss (on Debian buster)
RUN npm install -g rtlcss \
    && rm -Rf ~/.npm /tmp/*

FROM base AS builder

# Install hard & soft build dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    apt-utils dialog \
    apt-transport-https \
    build-essential \
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
    libsasl2-dev \
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
    && git -C /opt/odoo checkout --detach FETCH_HEAD \
    && pip3 install --prefix=/usr/local --no-cache-dir --upgrade --requirement /opt/odoo/requirements.txt \
    && pip3 -qq install --prefix=/usr/local --no-cache-dir --upgrade \
    rlpycairo \
    'websocket-client~=0.56' \
    packaging \
    phonenumbers \
    && apt-get autopurge -yqq \
    && rm -rf /var/lib/apt/lists/* /tmp/*

RUN pip3 install --editable /opt/odoo --config-settings editable_mode=compat \
    && rm -rf /var/lib/apt/lists/* /tmp/*

FROM base AS production

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

COPY --link --chown=${APP_UID}:${APP_GID} --from=builder /usr/local /usr/local
COPY --link --chown=${APP_UID}:${APP_GID} --from=builder /opt/odoo ${ODOO_BASEPATH}
COPY --link --chmod=0755 ./resources/entrypoint.sh ./resources/getaddons.py ./resources/dockerdoo_init.py /
# Add-ons to bake into the image, if any.
COPY --link --chown=${APP_UID}:${APP_GID} ./custom ${ODOO_EXTRA_ADDONS}

EXPOSE 8069 8072

HEALTHCHECK CMD curl --fail --silent "http://127.0.0.1:${ODOO_HTTP_PORT:-${PORT:-8069}}/web/health" || exit 1

# Starts as root so it can take ownership of a root-owned volume; the entrypoint then drops to the odoo user.
ENTRYPOINT ["/entrypoint.sh"]

CMD ["odoo"]
