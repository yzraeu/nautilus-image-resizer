# User install by default. Override for a system install, e.g.:
#   make install PREFIX=/usr/local
#   make install DESTDIR=/tmp/stage PREFIX=/usr
PREFIX ?= $(HOME)/.local
DESTDIR ?=

APP_SRC := nautilus-image-resizer/image_resizer.py
PLUGIN_SRC := nautilus/image_resizer_extension.py
DESKTOP_IN := data/dev.izzy.image-resizer.desktop.in
METAINFO_SRC := data/dev.izzy.image-resizer.metainfo.xml

APP_DST := $(DESTDIR)$(PREFIX)/share/nautilus-image-resizer/image_resizer.py
PLUGIN_DST := $(DESTDIR)$(PREFIX)/share/nautilus-python/extensions/image_resizer_extension.py
DESKTOP_DST := $(DESTDIR)$(PREFIX)/share/applications/dev.izzy.image-resizer.desktop
METAINFO_DST := $(DESTDIR)$(PREFIX)/share/metainfo/dev.izzy.image-resizer.metainfo.xml

.PHONY: test install uninstall clean

test:
	python3 -m py_compile $(APP_SRC) $(PLUGIN_SRC)
	python3 -m unittest discover -s tests -v

install: test
	install -Dm755 $(APP_SRC) $(APP_DST)
	install -Dm644 $(PLUGIN_SRC) $(PLUGIN_DST)
	install -Dm644 $(METAINFO_SRC) $(METAINFO_DST)
	sed 's|@APP_PATH@|$(PREFIX)/share/nautilus-image-resizer/image_resizer.py|' \
		$(DESKTOP_IN) > $(DESKTOP_DST)
	chmod 644 $(DESKTOP_DST)
	@if command -v update-desktop-database >/dev/null 2>&1; then \
		update-desktop-database $(DESTDIR)$(PREFIX)/share/applications || true; \
	fi
	@if command -v appstreamcli >/dev/null 2>&1; then \
		appstreamcli validate --no-net $(METAINFO_DST) || true; \
	fi
	@echo "Installed. Restart Files with: nautilus -q"

uninstall:
	rm -f $(APP_DST) $(PLUGIN_DST) $(DESKTOP_DST) $(METAINFO_DST)
	@echo "Removed. Restart Files with: nautilus -q"

clean:
	rm -rf __pycache__ tests/__pycache__ \
		nautilus-image-resizer/__pycache__ nautilus/__pycache__
	find . -name '*.pyc' -delete
