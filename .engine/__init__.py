"""Hermes Mod Engine package."""
from .models import Mod, ModTarget, ConfigField, MonitorSpec, ModCommand, CycleDetectionError
from .i18n import (
    OFFICIAL_LANGUAGES,
    DEFAULT_LANGUAGE,
    DEFAULT_CLI_LANGUAGE,
    normalize_language_id,
    is_language_id,
    is_supported_language,
    resolve_language,
    flatten_i18n_dict,
    compile_mod_locales,
    get_locales_cache_path,
    update_locales_cache,
    get_enabled_mod_locales,
    register_mod_locales,
    ENGINE_CLI_CATALOG,
    EngineI18nResolver,
    engine_i18n,
    t,
    get_language,
    get_active_language,
)
from .loader import (
    parse_mod_manifest,
    parse_mod_command,
    parse_inline_tests,
    reduce_mod_sections,
    _load_folder_mod_sections,
    update_commands_cache,
    update_mcp_cache,
    get_enabled_mod_skills_dirs,
    get_enabled_mod_mcp_servers,
    interpolate_mcp_config,
    resolve_mod_mcp_servers,
    _mod_mcp_cache_clear,
    ModDependencyGraph,
    ModLoader,
    is_valid_hermes_root,
    detect_hermes_root,
    HERMES_ROOT,
)
from .config_manager import ModConfigManager, SecretsStorage
from .monitor_manager import ModMonitorManager, TokenBucket

try:
    from .file_ops import FileLock, atomic_write_text, SignalTrap
except ImportError:
    class FileLock:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def atomic_write_text(p, content, encoding="utf-8"):
        Path(p).write_text(content, encoding=encoding)
    class SignalTrap:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass

try:
    from .version import ENGINE_VERSION, MIN_SUPPORTED_HERMES_VERSION
except ImportError:
    ENGINE_VERSION = "1.5.0"
    MIN_SUPPORTED_HERMES_VERSION = "0.20.0"

__version__ = ENGINE_VERSION

try:
    from .updater import (
        ModUpdater,
        parse_version,
        compare_versions,
        get_hermes_version,
        check_compatibility,
        CompatibilityIssue,
        StateManager,
        BackupManager,
    )
except ImportError:
    ModUpdater = None
    parse_version = None
    compare_versions = None
    get_hermes_version = None
    check_compatibility = None
    CompatibilityIssue = None
    StateManager = None
    BackupManager = None

try:
    from .pusher import ModPusher, handle_push_cli
except ImportError:
    ModPusher = None
    handle_push_cli = None

try:
    from .validator import ModValidator, ModValidationResult, ValidationIssue, CompositeSimulationReport
except ImportError:
    ModValidator = None
    ModValidationResult = None
    ValidationIssue = None
    CompositeSimulationReport = None

try:
    from .tester import ModTestRunner, TestResult, TestSuiteSummary
except ImportError:
    ModTestRunner = None
    TestResult = None
    TestSuiteSummary = None

try:
    from .test_cache import (
        TestCacheManager,
        compute_composite_fingerprint,
        compute_mod_hash,
        get_env_fingerprint,
    )
except ImportError:
    TestCacheManager = None
    compute_composite_fingerprint = None
    compute_mod_hash = None
    get_env_fingerprint = None

try:
    from .exporter import (
        ModExporter,
        ModImporter,
        ExportResult,
        ImportResult,
    )
except ImportError:
    ModExporter = None
    ModImporter = None
    ExportResult = None
    ImportResult = None

try:
    from .snapshot_manager import (
        SnapshotManager,
        SnapshotManifest,
        SnapshotFileRecord,
        RestoreResult,
    )
except ImportError:
    SnapshotManager = None
    SnapshotManifest = None
    SnapshotFileRecord = None
    RestoreResult = None

try:
    from .watchdog import (
        GatewayWatchdog,
        WatchdogResult,
        ServiceHealthStatus,
    )
except ImportError:
    GatewayWatchdog = None
    WatchdogResult = None
    ServiceHealthStatus = None

try:
    from .inspector import (
        ModInspector,
        is_system_mod,
        read_file_tail,
    )
except ImportError:
    ModInspector = None
    is_system_mod = None
    read_file_tail = None

try:
    from .circuit_breaker import (
        CircuitBreaker,
        get_circuit_breaker,
        CIRCUIT_STATE_CLOSED,
        CIRCUIT_STATE_OPEN,
        CIRCUIT_STATE_HALF_OPEN,
        DEFAULT_FAILURE_THRESHOLD,
        DEFAULT_WINDOW_SECONDS,
        DEFAULT_COOLDOWN_SECONDS,
    )
except ImportError:
    CircuitBreaker = None
    get_circuit_breaker = None
    CIRCUIT_STATE_CLOSED = "CLOSED"
    CIRCUIT_STATE_OPEN = "OPEN"
    CIRCUIT_STATE_HALF_OPEN = "HALF_OPEN"
    DEFAULT_FAILURE_THRESHOLD = 5
    DEFAULT_WINDOW_SECONDS = 300.0
    DEFAULT_COOLDOWN_SECONDS = 1800.0

__all__ = [
    "Mod",
    "ModTarget",
    "ConfigField",
    "MonitorSpec",
    "ModCommand",
    "OFFICIAL_LANGUAGES",
    "DEFAULT_LANGUAGE",
    "DEFAULT_CLI_LANGUAGE",
    "normalize_language_id",
    "is_language_id",
    "is_supported_language",
    "resolve_language",
    "update_commands_cache",
    "update_locales_cache",
    "update_mcp_cache",
    "compile_mod_locales",
    "get_enabled_mod_locales",
    "register_mod_locales",
    "flatten_i18n_dict",
    "get_locales_cache_path",
    "ENGINE_CLI_CATALOG",
    "EngineI18nResolver",
    "engine_i18n",
    "t",
    "get_language",
    "get_active_language",
    "CycleDetectionError",
    "ModDependencyGraph",
    "ModLoader",
    "parse_mod_manifest",
    "parse_mod_command",
    "_load_folder_mod_sections",
    "get_enabled_mod_skills_dirs",
    "get_enabled_mod_mcp_servers",
    "interpolate_mcp_config",
    "resolve_mod_mcp_servers",
    "_mod_mcp_cache_clear",
    "ModConfigManager",
    "SecretsStorage",
    "ModMonitorManager",
    "TokenBucket",
    "ENGINE_VERSION",
    "MIN_SUPPORTED_HERMES_VERSION",
    "ModUpdater",
    "ModPusher",
    "handle_push_cli",
    "parse_version",
    "compare_versions",
    "get_hermes_version",
    "check_compatibility",
    "CompatibilityIssue",
    "StateManager",
    "BackupManager",
    "ModValidator",
    "ModValidationResult",
    "ValidationIssue",
    "CompositeSimulationReport",
    "FileLock",
    "atomic_write_text",
    "SignalTrap",
    "ModTestRunner",
    "TestResult",
    "TestSuiteSummary",
    "TestCacheManager",
    "compute_composite_fingerprint",
    "compute_mod_hash",
    "get_env_fingerprint",
    "ModExporter",
    "ModImporter",
    "ExportResult",
    "ImportResult",
    "SnapshotManager",
    "SnapshotManifest",
    "SnapshotFileRecord",
    "RestoreResult",
    "GatewayWatchdog",
    "WatchdogResult",
    "ServiceHealthStatus",
    "ModInspector",
    "is_system_mod",
    "read_file_tail",
    "CircuitBreaker",
    "get_circuit_breaker",
    "CIRCUIT_STATE_CLOSED",
    "CIRCUIT_STATE_OPEN",
    "CIRCUIT_STATE_HALF_OPEN",
    "__version__",
    "is_valid_hermes_root",
    "detect_hermes_root",
    "HERMES_ROOT",
]
