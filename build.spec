# -*- mode: python ; coding: utf-8 -*-

block_cipher = None


def build_config_datas() -> list:
    """Collect configuration template files for bundling."""
    return [('config.yaml', '.')]


def build_analysis() -> Analysis:
    return Analysis(
        ['main.py'],
        pathex=[],
        binaries=[],
        datas=build_config_datas(),
        hiddenimports=[
            'jieba',
            'jieba.analyse',
            'jieba.finalseg',
            'jieba.posseg',
            'jieba.ltokenizer',
            'jieba.lcut',
            'jieba.load_userdict',
            'jieba.cut',
            'jieba.cut_for_search',
            'markdown_it',
            'markdown_it.rules_block',
            'markdown_it.rules_inline',
            'markdown_it.token',
            'markdown_it.common.utils',
            'markdown_it.common.normalize_url',
            'markdown_it.common.assign',
            'markdown_it.renderer',
            'markdown_it.presets.commonmark',
            'pypinyin',
        ],
        hookspath=[],
        hooksconfig={
            'jieba': {
                'include_default_dict': True,
            }
        },
        runtime_hooks=[],
        excludes=[],
        cipher=block_cipher,
        noarchive=False,
    )


def build_exe(analysis: Analysis, pyz: PYZ) -> EXE:
    return EXE(
        pyz,
        name='echo-client',
        console=True,
        disable_windowed_traceback=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        argv_emulation=False,
    )


analysis = build_analysis()
pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)
exe = build_exe(analysis, pyz)
coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='echo-client',
)
