import os
import io
import sys
import json
import subprocess
import shutil
from pathlib import Path
from xml.dom import minidom
from contextlib import contextmanager
import urllib.request
import urllib.parse
import tempfile
import re
import hashlib
import stat
import inspect


@contextmanager
def pushd(new_dir):
    previous_dir = os.getcwd()
    os.chdir(new_dir)
    try:
        yield
    finally:
        os.chdir(previous_dir)


def call(*args, output=False, env=None, **kwargs):
    env = None if env is None else {**os.environ.copy(), **env}
    if output:
        return subprocess.run(args, stdout=subprocess.PIPE, universal_newlines=True, check=True, env=env, **kwargs).stdout
    else:
        subprocess.run(args, check=True, env=env, **kwargs)


def inline(text):
    return ' '.join(text.split())


def httpget(*args, **kwargs):
    return urllib.request.urlopen(urllib.request.Request(*args, **kwargs)).read()


def get_url_sha512(url):
    sha512 = hashlib.sha512()
    sha512.update(httpget(url, headers={'Accept': 'application/octet-stream'}))
    return {
        'url': url,
        'sha512': sha512.hexdigest()
    }


def load_lockfile():
    result = None
    script = "console.log(JSON.stringify(require('@yarnpkg/lockfile').parse(require('fs').readFileSync(process.stdin.fd, 'utf8')).object))"
    with tempfile.TemporaryDirectory() as tmp:
        call('npm', 'install', '--no-save', '@yarnpkg/lockfile', cwd=tmp)
        while True:
            path = Path((yield result))
            with path.open() as fd:
                result = json.loads(call('node', '-e', script, stdin=fd, cwd=tmp, output=True))


def get_yarn_recipe():
    url = json.loads(httpget('https://api.github.com/repos/yarnpkg/yarn/releases/latest').decode())['assets'][1]['browser_download_url']

    return {
        'type': 'file',
        **get_url_sha512(url),
        'dest': 'bin',
        'dest-filename': 'yarn.js'
    }


def get_imagemagick_archive():
    version_pattern = re.compile(r'ImageMagick-(\d+)\.(\d+)\.(\d+)-(\d+)\.(.+)')

    def version_key(version):
        version = version_pattern.fullmatch(version[0]).groups()
        return (version[4] == 'tar.xz', *(int(number) for number in version[0:4]))

    contents = [content for content in minidom.parseString(
        httpget('https://www.imagemagick.org/download/releases/digest.rdf')
    ).documentElement.childNodes if content.nodeName == 'digest:Content']
    releases = [(
        content.attributes['rdf:about'].value,
        next(node.firstChild.data for node in content.childNodes if node.nodeName == 'digest:sha256')
    ) for content in contents]
    latest = max(releases, key=version_key)
    return {
        'type': 'archive',
        'url': 'https://www.imagemagick.org/download/releases/' + latest[0],
        'sha256': latest[1]
    }


def get_git_with_tag(url, tag):
    stream = io.TextIOWrapper(urllib.request.urlopen(url + '/info/refs?service=git-upload-pack'))
    refs = {}
    while True:
        line = stream.readline()
        line = line[4:]
        if line == '':
            break
        if line.startswith('#'):
            continue
        line = line.split('\0')[0]
        line = line.split(' ')
        refs[line[1].strip()] = line[0]
    return {
        'type': 'git',
        'url': url,
        'tag': tag,
        'commit': refs.get('refs/tags/' + tag + '^{}', refs.get('refs/tags/' + tag))
    }


def get_python_packages():
    packages = ['autopep8', 'pylint', 'pipenv', 'ipython', 'rope']
    sources = []
    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run('eval "$(pyenv init -)"; pyenv install -s 3.5.2; pyenv shell 3.5.2; pip3 download -d' + tmpdir + ' ' + ' '.join(packages), shell=True)
        for filename in sorted(os.listdir(tmpdir)):
            if filename.endswith('.whl'):
                package = re.fullmatch(r'(.*)-.*?-.*?-.*?-.*?\.whl', filename).groups()[0]
            elif filename.endswith('.tar.gz'):
                package = re.fullmatch(r'(.*)-.*?\.tar\.gz', filename).groups()[0]
            elif filename.endswith('.zip'):
                package = re.fullmatch(r'(.*)-.*?\.zip', filename).groups()[0]
            else:
                continue
            dom = minidom.parseString(httpget('https://pypi.org/simple/' + package + '/'))
            url = next(node.getAttribute('href') for node in dom.getElementsByTagName('a') if node.firstChild.data == filename)
            sources.append({
                'type': 'file',
                'dest-filename': filename,
                'url': urllib.parse.urldefrag(url)[0],
                **dict([urllib.parse.urldefrag(url)[1].split('=')])
            })
        return {
            'name': 'python_packages',
            'buildsystem': 'simple',
            'build-commands': [
                    'PYTHONUSERBASE=/app pip3 install --user --no-index --find-links . ' + ' '.join(packages),
            ],
            'sources': sources
        }


def parse_repo():
    loader = load_lockfile()
    next(loader)
    releases = json.loads(httpget('https://vscode-update.azurewebsites.net/api/releases/stable', headers={'X-API-Version': '2'}).decode())
    with tempfile.TemporaryDirectory() as tmp, pushd(tmp):
        call('git', 'clone', '--branch', releases[0]['version'], 'https://github.com/Microsoft/vscode.git', '.')
        releases = [{
            **release,
            'date': inline(call('git', 'show', '-s', '--format=%cd', '--date=iso-strict-local', release['id'], env={
                'TZ': 'UTC'
            }, output=True))
        } for release in releases if release['version'].split('.')[0] != '0']
        product_json = json.loads(Path('product.json').read_text())

        re_node_version = re.compile(r'(.*)@.*?')
        packages = {}
        for lockpath in Path().glob('**/yarn.lock'):
            lockfile = loader.send(lockpath)
            for entry in lockfile:
                resolved = urllib.parse.urldefrag(lockfile[entry]['resolved'])
                name = re_node_version.match(entry).group(1)
                version = lockfile[entry]['version']
                if resolved[1] == '':
                    packages[(name, version)] = {
                        'type': 'file',
                        **get_url_sha512(resolved[0]),
                        'dest': 'yarn-mirror',
                        'dest-filename': resolved[0].split('/')[-1]
                    }
                else:
                    packages[(name, version)] = {
                        'type': 'file',
                        'url': resolved[0],
                        'sha1': resolved[1],
                        'dest': 'yarn-mirror',
                        'dest-filename': name.replace('/', '-') + '-' + version + '.tgz'
                    }
        return loader.send('.yarnrc')['target'], packages, {
            '@comments': {
                'NOTICE': 'This file is auto-generated, do not modify',
                'releases': [{
                    'version': release['version'],
                    'date': release['date'][:-6]
                } for release in releases]
            },
            'app-id': product_json['darwinBundleIdentifier'],
            'branch': 'stable',
            'command': product_json['applicationName'],
            'separate-locales': False,
            'finish-args': [
                '--share=ipc',
                '--socket=x11',
                '--socket=pulseaudio',
                '--share=network',
                '--device=dri',
                '--filesystem=host',
                '--persist=' + product_json['dataFolderName'],
                '--talk-name=org.freedesktop.Notifications',
                '--env=PYTHONPATH=/app/lib/python3.5/site-packages'
            ],
            'modules': [
                {
                    'name': 'libsecret',
                    'config-opts': [
                        '--disable-manpages',
                        '--disable-gtk-doc',
                        '--disable-static',
                        '--disable-introspection'
                    ],
                    'cleanup': [
                        '/bin',
                        '/include',
                        '/lib/pkgconfig',
                        '/share/gtk-doc',
                        '*.la'
                    ],
                    'sources': [
                        get_git_with_tag('https://git.gnome.org/browse/libsecret.git', '0.18.5')
                    ]
                },
                {
                    'name': 'libxkbfile',
                    'cleanup': [
                        '/include',
                        '/lib/*.la',
                        '/lib/pkgconfig'
                    ],
                    'config-opts': [
                        '--disable-static'
                    ],
                    'sources': [
                        get_git_with_tag('https://anongit.freedesktop.org/git/xorg/lib/libxkbfile.git', 'libxkbfile-1.0.9')
                    ]
                },
                {
                    'name': 'ImageMagick',
                    'build-options': {
                        'prefix': '/app/local'
                    },
                    'cleanup': [
                        '/local'
                    ],
                    'sources': [
                        get_imagemagick_archive()
                    ],
                    'config-opts': [
                        '--enable-static=no',
                        '--with-modules',
                        '--disable-docs',
                        '--disable-deprecated',
                        '--without-autotrace',
                        '--without-bzlib',
                        '--without-djvu',
                        '--without-dps',
                        '--without-fftw',
                        '--without-fontconfig',
                        '--without-fpx',
                        '--without-freetype',
                        '--without-gvc',
                        '--without-jbig',
                        '--without-jpeg',
                        '--without-lcms',
                        '--without-lzma',
                        '--without-magick-plus-plus',
                        '--without-openexr',
                        '--without-openjp2',
                        '--without-pango',
                        '--without-raqm',
                        '--without-tiff',
                        '--without-webp',
                        '--without-wmf',
                        '--without-x',
                        '--without-xml',
                        '--without-zlib'
                    ]
                },
                {
                    'name': 'node',
                    'build-options': {
                        'prefix': '/app/local'
                    },
                    'cleanup': [
                        '/local'
                    ],
                    'sources': [
                        {
                            'type': 'archive',
                            **get_url_sha512('https://nodejs.org/dist/v8.9.1/node-v8.9.1.tar.xz')
                        }
                    ],
                    'post-install': [
                        'python -m compileall /app/local/lib/node_modules/npm/node_modules/node-gyp'
                    ]
                },
                {
                    'name': 'vscode',
                    'buildsystem': 'simple',
                    'build-options': {
                        'append-path': '/app/local/bin'
                    },
                    'build-commands': [
                        'python3 build.py',
                    ],
                    'cleanup': [
                        '/local'
                    ],
                    'sources': [
                        {
                            'type': 'git',
                            'url': 'https://github.com/Microsoft/vscode.git',
                            'tag': releases[0]['version'],
                            'commit': releases[0]['id'],
                            'dest': 'vscode',
                            'disable-shallow-clone': True
                        },
                        {
                            'type': 'script',
                            'commands': [
                                'import os',
                                'import sys',
                                'import json',
                                'import subprocess',
                                'import shutil',
                                'from pathlib import Path',
                                'from xml.dom import minidom',
                                'from contextlib import contextmanager',
                                'import urllib.request',
                                'import urllib.parse',
                                'import tempfile',
                                'import re',
                                'import hashlib',
                                'import stat',
                                *inspect.getsource(build).split('\n'),
                                'build()'
                            ],
                            'dest-filename': 'build.py'
                        },
                        {
                            'type': 'file',
                            'path': product_json['darwinBundleIdentifier'] + '.json'
                        },
                        {
                            'type': 'file',
                            **get_url_sha512('https://raw.githubusercontent.com/Microsoft/vscode/b00945fc8c79f6db74b280ef53eba060ed9a1388/product.json')
                        },
                        *packages.values()
                    ]
                },
                get_python_packages()
            ]
        }


def get_electron_recipe(packages, iojs_version):
    def patch_zero(version):
        parts = version.split('.')
        parts[-1] = '0'
        return '.'.join(parts)

    electrons = []
    electron_recipe = []
    sha256sums = {}
    electrons.extend([('mksnapshot', patch_zero(package[1]), '.electron') for package in packages if package[0] == 'electron-mksnapshot'])
    electrons.extend([('chromedriver', patch_zero(package[1]), '.electron') for package in packages if package[0] == 'electron-chromedriver'])
    electrons.extend([('electron', package[1], '.electron') for package in packages if package[0] == 'electron'])
    electrons.append(('electron', iojs_version, 'gulp-electron-cache/atom/electron'))
    electrons.append(('ffmpeg', iojs_version, 'gulp-electron-cache/atom/electron'))
    for name, version, dest in electrons:
        if version not in sha256sums:
            sha256sums[version] = httpget('https://github.com/electron/electron/releases/download/v' + version + '/SHASUMS256.txt')
        for arch_linux, arch_node in [
            ('x86_64', 'x64'),
            ('i386', 'ia32'),
            ('arm', 'arm')
        ]:
            filename = name + '-v' + version + '-linux-' + arch_node + '.zip'
            electron_recipe.append({
                'type': 'file',
                'url': 'https://github.com/electron/electron/releases/download/v' + version + '/' + filename,
                'sha256': next(line.split(' ')[0] for line in sha256sums[version].decode().split('\n') if filename in line),
                'only-arches': [arch_linux],
                'dest': dest,
                'dest-filename': filename,
                '@comment': {
                    'version': version
                }
            })
    electron_recipe.append({
        'type': 'file',
        **get_url_sha512('https://atom.io/download/electron/v' + iojs_version + '/iojs-v' + iojs_version + '.tar.gz'),
        'dest': 'misc',
        'dest-filename': 'iojs.tar.gz'
    })
    return electron_recipe


def get_ripgrep_recipe(packages):
    version = next(package[1] for package in packages if package[0] == 'vscode-ripgrep')
    url = 'https://cdn.jsdelivr.net/npm/vscode-ripgrep@' + version + '/lib/postinstall.js'
    line = next(line for line in httpget(url).decode().split('\n') if line.startswith('const version'))
    line += ';console.log(version)'
    version = inline(call('node', '-e', line, output=True))
    return [{
        'type': 'file',
        **get_url_sha512('https://github.com/roblourens/ripgrep/releases/download/' + version + '/ripgrep-' + version + '-linux-' + arch_node + '.zip'),
        'only-arches': [
            arch_linux
        ],
        'dest': 'misc',
        'dest-filename': 'ripgrep.zip'
    } for arch_linux, arch_node in [
        ('x86_64', 'x64'),
        ('i386', 'ia32'),
        ('arm', 'arm')
    ]]


def get_base_recipe():
    base = json.loads(httpget('https://github.com/flathub/io.atom.electron.BaseApp/raw/master/io.atom.electron.BaseApp.json').decode())
    return {
        'base': base['id'],
        'base-version': base['branch'],
        # 'runtime': base['runtime'],
        'runtime': base['sdk'],
        'runtime-version': base['runtime-version'],
        'sdk': base['sdk']
    }


def generate_recipe():
    iojs_version, packages, recipe = parse_repo()
    recipe.update(get_base_recipe())
    sources = next(module for module in recipe['modules'] if module['name'] == 'vscode')['sources']
    sources.append(get_yarn_recipe())
    sources.extend(get_electron_recipe(packages, iojs_version))
    sources.extend(get_ripgrep_recipe(packages))
    return recipe


def build():
    product = json.loads(Path('vscode/product.json').read_text())
    recipe = json.loads(Path(os.environ['FLATPAK_ID'] + '.json').read_text())
    arch = ' '.join(subprocess.run(['node', '-e', 'console.log(process.arch)'], stdout=subprocess.PIPE, universal_newlines=True).stdout.split())

    sha256sums = {}
    for package in [source for source in next(
        module for module in recipe['modules'] if module['name'] == 'vscode'
    )['sources'] if source.get('dest') == '.electron']:
        if package['@comment']['version'] not in sha256sums:
            sha256sums[package['@comment']['version']] = {}
        sha256sums[package['@comment']['version']][package['dest-filename']] = package['sha256']
    for version in sha256sums:
        Path('.electron/SHASUMS256.txt-' + version).write_text('\n'.join(
            sha256sums[version][filename] + ' *' + filename for filename in sha256sums[version])
        )

    shutil.move('gulp-electron-cache', '/tmp')
    shutil.move('.electron', str(Path.home()))
    shutil.move('bin/yarn.js', '/app/local/bin')
    Path('/app/local/bin/yarn.js').chmod(Path('/app/local/bin/yarn.js').stat().st_mode | stat.S_IXUSR)
    Path('/app/local/bin/yarn').symlink_to('yarn.js')
    subprocess.run(['yarn', 'config', 'set', 'yarn-offline-mirror', str(Path('yarn-mirror').resolve())], check=True)

    shutil.unpack_archive(str(next(Path('yarn-mirror').glob('vscode-ripgrep-*'))))
    shutil.move('package', 'vscode-ripgrep')
    subprocess.run(['yarn', 'link'], check=True, cwd='vscode-ripgrep')
    Path('vscode-ripgrep/bin').mkdir()
    shutil.unpack_archive('misc/ripgrep.zip', 'vscode-ripgrep/bin')
    Path('vscode-ripgrep/bin/rg').chmod(Path('vscode-ripgrep/bin/rg').stat().st_mode | stat.S_IXUSR)

    os.chdir('vscode')
    Path('product.json').write_text(json.dumps({
        **json.loads(Path('product.json').read_text()),
        'extensionsGallery': json.loads(Path('../product.json').read_text())['extensionsGallery']
    }))
    Path('build/builtInExtensions.json').write_text('[]')
    subprocess.run(['yarn', 'link', 'vscode-ripgrep'], check=True)
    package_vscode_extension = json.loads(Path('extensions/vscode-colorize-tests/package.json').read_text())
    del package_vscode_extension['scripts']['postinstall']
    Path('extensions/vscode-colorize-tests/package.json').write_text(json.dumps(package_vscode_extension))

    subprocess.run(['yarn', 'install', '--offline', '--verbose', '--frozen-lockfile'], check=True, env={
        **os.environ,
        'npm_config_tarball': str(Path('../misc/iojs.tar.gz').resolve()),
    })

    Path('node_modules/vscode-ripgrep').unlink()
    Path('../vscode-ripgrep').rename('node_modules/vscode-ripgrep')
    shutil.copy('src/vs/vscode.d.ts', 'extensions/vscode-colorize-tests/node_modules/vscode')
    subprocess.run(['node_modules/.bin/gulp', 'vscode-linux-' + arch + '-min', '--max_old_space_size=4096'], check=True)

    os.chdir('..')
    shutil.move('VSCode-linux-' + arch, '/app/share/' + product['applicationName'])
    os.symlink('../share/' + product['applicationName'] + '/bin/' + product['applicationName'], '/app/bin/' + product['applicationName'])
    Path('/app/share/icons/hicolor/1024x1024/apps').mkdir(parents=True)
    shutil.copy('vscode/resources/linux/code.png', '/app/share/icons/hicolor/1024x1024/apps/' + os.environ['FLATPAK_ID'] + '.png')
    for size in [16, 24, 32, 48, 64, 128, 192, 256, 512]:
        size = str(size)
        Path('/app/share/icons/hicolor/' + size + 'x' + size + '/apps').mkdir(parents=True)
        Path('/app/share/icons/hicolor/' + size + 'x' + size + '/apps/' + os.environ['FLATPAK_ID'] + '.png').write_bytes(subprocess.run([
            'magick',
            'convert',
            'vscode/resources/linux/code.png',
            '-resize',
            size + 'x' + size,
            '-'
        ], check=True, stdout=subprocess.PIPE).stdout)

    Path('/app/share/applications').mkdir(parents=True)
    Path('/app/share/applications/' + os.environ['FLATPAK_ID'] + '.desktop').write_text(
        Path('vscode/resources/linux/code.desktop')
        .read_text()
        .replace('Exec=/usr/share/@@NAME@@/@@NAME@@', 'Exec=' + product['applicationName'])
        .replace('@@NAME_LONG@@', product['nameLong'])
        .replace('@@NAME_SHORT@@', product['nameShort'])
        .replace('@@NAME@@', os.environ['FLATPAK_ID'])
        .replace('@@ICON@@', os.environ['FLATPAK_ID'])
    )

    dom = minidom.parse('vscode/resources/linux/code.appdata.xml')

    def remove_white(node):
        if node.nodeType == minidom.Node.TEXT_NODE and node.data.strip() == '':
            node.data = ''
        else:
            list(map(remove_white, node.childNodes))

    remove_white(dom)
    releases = dom.createElement('releases')
    for entry in recipe['@comments']['releases']:
        release = dom.createElement('release')
        release.setAttribute('version', entry['version'])
        release.setAttribute('date', entry['date'])
        releases.appendChild(release)
    dom.getElementsByTagName('component')[0].appendChild(releases)
    description_paragraph = dom.createElement('p')
    description_paragraph.appendChild(dom.createTextNode(re.sub(r'\s+', r' ', '''
        The above paragraph, from upstream Microsoft, is the same for this OSS version and the non-OSS
        version https://flathub.org/apps/details/com.visualstudio.code. The difference between them is
        described at https://github.com/flathub/com.visualstudio.code.oss/issues/6#issuecomment-380152999.
        This version is compiled directly from the source code provided in the upstream GitHub repository
        with minor modifications, as the official binary is licensed proprietarily. Essential features
        are all present.
    '''.strip())))
    dom.getElementsByTagName('description')[0].appendChild(description_paragraph)
    lines = dom.toxml(encoding='UTF-8').decode()
    Path('/app/share/appdata').mkdir(parents=True)
    Path('/app/share/appdata/' + os.environ['FLATPAK_ID'] + '.appdata.xml').write_text(
        lines
        .replace('@@NAME_LONG@@', product['nameLong'])
        .replace('@@NAME@@', os.environ['FLATPAK_ID'])
        .replace('@@LICENSE@@', product['licenseName'])
    )


def main():
    recipe = generate_recipe()
    Path(recipe['app-id'] + '.json').write_text(json.dumps(recipe, indent=2) + '\n')


if __name__ == '__main__':
    main()
