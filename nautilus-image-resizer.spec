Name:           nautilus-image-resizer
Version:        1.0.1
Release:        1%{?dist}
Summary:        Resize and crop images from the GNOME Files right-click menu

License:        GPL-3.0-or-later
URL:            https://github.com/yzraeu/nautilus-image-resizer
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz#/%{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib
BuildRequires:  make
BuildRequires:  python3-pillow

Requires:       gtk4
Requires:       libadwaita
Requires:       nautilus
Requires:       nautilus-python
Requires:       python3
Requires:       python3-gobject
Requires:       python3-pillow

%description
Adds a Resize Image entry to the right-click menu in GNOME Files
(Nautilus). Pick a size by percent, by dimensions, or by target file
size, crop with aspect presets if needed, and save over the original
or as a copy. A live preview shows the result with an output size estimate before you save. Formats: JPEG, PNG, WEBP, BMP, TIFF.

%prep
%autosetup -n %{name}-%{version}

%build
# Nothing to compile: three Python files, a desktop file and metainfo.

%install
%make_install PREFIX=%{_prefix}
desktop-file-validate %{buildroot}%{_datadir}/applications/dev.izzy.image-resizer.desktop
appstream-util validate-relax --nonet %{buildroot}%{_datadir}/metainfo/dev.izzy.image-resizer.metainfo.xml

%check
make test

%files
%license LICENSE
%doc README.md docs
%{_datadir}/nautilus-image-resizer/image_resizer.py
%{_datadir}/nautilus-image-resizer/resize_engine.py
%{_datadir}/nautilus-python/extensions/image_resizer_extension.py
%{_datadir}/applications/dev.izzy.image-resizer.desktop
%{_datadir}/metainfo/dev.izzy.image-resizer.metainfo.xml
%{_datadir}/nautilus-image-resizer/icons/hicolor/index.theme
%{_datadir}/nautilus-image-resizer/icons/hicolor/scalable/actions/percent-symbolic.svg
%{_datadir}/nautilus-image-resizer/icons/hicolor/scalable/actions/file-size-symbolic.svg

%changelog
* Tue Nov 17 2026 yzraeu <yzraeu@gmail.com> - 1.0.1-1
- Multi-image stacks, scattered photo preview, faster startup and estimates
- Keyboard and screen reader support, accessibility fixes
- Store listing: screenshots, keywords, Graphics category

* Sun Nov 15 2026 yzraeu <yzraeu@gmail.com> - 1.0.0-1
- Initial package
