Name:           nautilus-image-resizer
Version:        1.0.0
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
or as a copy. Formats: JPEG, PNG, WEBP, BMP, TIFF.

%prep
%autosetup -n %{name}-%{version}

%build
# Nothing to compile: two Python scripts, a desktop file and metainfo.

%install
%make_install PREFIX=%{_prefix}
desktop-file-validate %{buildroot}%{_datadir}/applications/dev.izzy.image-resizer.desktop
appstream-util validate-relax --nonet %{buildroot}%{_datadir}/metainfo/dev.izzy.image-resizer.metainfo.xml

%check
make test

%files
%license LICENSE
%doc README.md
%{_datadir}/nautilus-image-resizer/image_resizer.py
%{_datadir}/nautilus-python/extensions/image_resizer_extension.py
%{_datadir}/applications/dev.izzy.image-resizer.desktop
%{_datadir}/metainfo/dev.izzy.image-resizer.metainfo.xml

%changelog
* Sun Nov 15 2026 Israel Rodriguez <yzraeu@gmail.com> - 1.0.0-1
- Initial package
