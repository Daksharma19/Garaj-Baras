import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'api.dart';

// ── Colours ──────────────────────────────────────────────────────────────────
const _bg = Color(0xFF05101F);
const _card = Color(0xFF0D1B2E);
const _input = Color(0xFF1A2840);
const _accent = Color(0xFF38BDF8);
const _textPrimary = Colors.white;
const _textSecondary = Color(0xFF94A3B8);
const _dotSrc = Color(0xFF38BDF8);
const _dotDst = Color(0xFFF59E0B);
const _green = Color(0xFF22C55E);
const _red = Color(0xFFEF4444);

Color _rainColor(String label) {
  if (label.contains('Very Heavy')) return _red;
  if (label.contains('Heavy')) return const Color(0xFFF59E0B);
  if (label.contains('Moderate')) return const Color(0xFF0EA5E9);
  if (label.contains('Very Light')) return const Color(0xFF7DD3FC);
  if (label.contains('Light')) return _accent;
  return Colors.white;
}

String _rainGroup(String label) {
  if (label == 'No Rain') return 'No Rain';
  if (label.contains('Very Light') || label.contains('Light')) return 'Light';
  if (label.contains('Moderate')) return 'Medium';
  if (label.contains('Heavy')) return 'Heavy';
  return 'No Rain';
}

Color _patchColor(String intensity) => switch (intensity) {
      'Heavy' => const Color(0xFFF59E0B),
      'Medium' => const Color(0xFF0EA5E9),
      _ => _accent,
    };

String _toIST(double etaMins) {
  final t = DateTime.now()
      .toUtc()
      .add(const Duration(hours: 5, minutes: 30))
      .add(Duration(minutes: etaMins.round()));
  return '${t.hour.toString().padLeft(2, '0')}:${t.minute.toString().padLeft(2, '0')}';
}

// ── Rain Patch model ──────────────────────────────────────────────────────────
class _Patch {
  final double startMin, endMin;
  final String intensity, decayStatus;
  const _Patch(this.startMin, this.endMin, this.intensity, this.decayStatus);
}

List<_Patch> _computePatches(List<dynamic> waypoints) {
  final sorted = waypoints
      .cast<Map>()
      .where((w) => w['eta_mins'] != null)
      .toList()
    ..sort((a, b) =>
        (a['eta_mins'] as num).compareTo(b['eta_mins'] as num));

  final patches = <_Patch>[];
  double? start, last;
  final buf = <Map>[];

  for (final wp in sorted) {
    final eta = (wp['eta_mins'] as num).toDouble();
    if (wp['rain_expected'] == true) {
      start ??= eta;
      last = eta;
      buf.add(wp);
    } else if (start != null) {
      patches.add(_makePatch(start!, last!, buf));
      start = null;
      last = null;
      buf.clear();
    }
  }
  if (start != null) patches.add(_makePatch(start!, last!, buf));
  return patches;
}

_Patch _makePatch(double s, double e, List<Map> wps) {
  final labels = wps.map((w) => _rainGroup(w['label']?.toString() ?? '')).toList();
  final intensity = labels.contains('Heavy')
      ? 'Heavy'
      : labels.contains('Medium')
          ? 'Medium'
          : 'Light';
  final decays =
      wps.map((w) => w['decay_status']?.toString()).whereType<String>().toList();
  final decay = decays.contains('dead')
      ? 'dead'
      : decays.contains('dying')
          ? 'dying'
          : decays.contains('weakening')
              ? 'weakening'
              : 'stable';
  return _Patch(s, e, intensity, decay);
}

// ── Screen ────────────────────────────────────────────────────────────────────
enum _State { planner, loading, scanning, results }

class RouteScreen extends StatefulWidget {
  const RouteScreen({super.key});

  @override
  State<RouteScreen> createState() => _RouteScreenState();
}

class _RouteScreenState extends State<RouteScreen> {
  final _srcCtrl = TextEditingController();
  final _dstCtrl = TextEditingController();
  final _speedCtrl = TextEditingController();

  PlaceSuggestion? _srcPlace, _dstPlace;
  List<PlaceSuggestion> _srcSug = [], _dstSug = [];
  bool _srcOpen = false, _dstOpen = false;
  Timer? _srcTimer, _dstTimer;

  _State _state = _State.planner;
  String _scanStatus = '';
  String? _error;
  Map<String, dynamic>? _result;
  String _routeName = '';
  double? _distanceKm;

  @override
  void initState() {
    super.initState();
    ApiService.warmBackend();
  }

  @override
  void dispose() {
    _srcCtrl.dispose();
    _dstCtrl.dispose();
    _speedCtrl.dispose();
    _srcTimer?.cancel();
    _dstTimer?.cancel();
    super.dispose();
  }

  void _debounceSearch({
    required String value,
    required Timer? existing,
    required void Function(Timer) save,
    required void Function(List<PlaceSuggestion>) onResult,
    required void Function(bool) setOpen,
  }) {
    existing?.cancel();
    if (value.trim().length < 2) {
      setState(() {
        onResult([]);
        setOpen(false);
      });
      return;
    }
    final t = Timer(const Duration(milliseconds: 350), () async {
      final sug = await ApiService.searchPlaces(value);
      if (mounted) {
        setState(() {
          onResult(sug);
          setOpen(sug.isNotEmpty);
        });
      }
    });
    save(t);
  }

  Future<void> _handleScan() async {
    final src = _srcCtrl.text.trim();
    final dst = _dstCtrl.text.trim();
    final speed = double.tryParse(_speedCtrl.text.trim());
    if (src.isEmpty || dst.isEmpty) {
      setState(() => _error = 'Please enter both Source and Destination.');
      return;
    }
    if (speed == null || speed <= 0) {
      setState(() => _error = 'Please enter a valid average speed (km/h).');
      return;
    }

    setState(() {
      _state = _State.loading;
      _error = null;
      _result = null;
    });

    try {
      PlaceSuggestion srcP = _srcPlace ??
          (await ApiService.searchPlaces(src)).firstOrNull ??
          (throw Exception('Could not find "$src". Try a more specific name.'));
      PlaceSuggestion dstP = _dstPlace ??
          (await ApiService.searchPlaces(dst)).firstOrNull ??
          (throw Exception('Could not find "$dst". Try a more specific name.'));

      _distanceKm = haversineKm(srcP.lat, srcP.lon, dstP.lat, dstP.lon);
      _routeName = '${srcP.shortName} → ${dstP.shortName}';

      final wps =
          sampleRoute(srcP.lat, srcP.lon, dstP.lat, dstP.lon, speed);
      if (wps.isEmpty) throw Exception('Could not sample route waypoints.');

      setState(() {
        _state = _State.scanning;
        _scanStatus = 'Scanning radar…';
      });

      final result = await ApiService.predictWaypoints(wps);
      setState(() {
        _result = result;
        _state = _State.results;
      });
    } catch (e) {
      setState(() {
        _state = _State.planner;
        _error = e.toString().replaceAll('Exception: ', '');
      });
    }
  }

  void _handleBack() => setState(() {
        _state = _State.planner;
        _result = null;
        _error = null;
        _distanceKm = null;
      });

  // ── Build ─────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) => switch (_state) {
        _State.planner => _buildPlanner(),
        _State.loading || _State.scanning => _buildLoading(),
        _State.results => _buildResults(),
      };

  // ── Planner ───────────────────────────────────────────────────────────────

  Widget _buildPlanner() {
    final canScan = _srcCtrl.text.trim().isNotEmpty &&
        _dstCtrl.text.trim().isNotEmpty &&
        _speedCtrl.text.trim().isNotEmpty;

    return SafeArea(
      child: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(16, 16, 16, 80),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _NavBar(),
            const SizedBox(height: 20),
            // Hero
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(24),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(16),
                gradient: const LinearGradient(
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                  colors: [_card, Color(0xFF1A2840)],
                ),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text(
                    'Know the rain\nbefore you leave.',
                    style: TextStyle(
                      color: _textPrimary,
                      fontSize: 24,
                      fontWeight: FontWeight.bold,
                      height: 1.2,
                    ),
                  ),
                  const SizedBox(height: 8),
                  Text(
                    'Delhi NCR & UP · IMD radar · Route-aware',
                    style: TextStyle(
                        color: Colors.white.withOpacity(0.55), fontSize: 13),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 16),
            // Planner card
            Container(
              padding: const EdgeInsets.all(20),
              decoration: BoxDecoration(
                color: _card,
                borderRadius: BorderRadius.circular(16),
                border:
                    Border.all(color: Colors.white.withOpacity(0.06)),
              ),
              child: Column(
                children: [
                  // FROM field
                  _LocationField(
                    label: 'FROM',
                    controller: _srcCtrl,
                    suggestions: _srcSug,
                    showDropdown: _srcOpen,
                    placeholder: 'Starting city',
                    dotColor: _dotSrc,
                    onChanged: (v) => _debounceSearch(
                      value: v,
                      existing: _srcTimer,
                      save: (t) => _srcTimer = t,
                      onResult: (s) => _srcSug = s,
                      setOpen: (b) => _srcOpen = b,
                    ),
                    onSelect: (p) => setState(() {
                      _srcCtrl.text = p.displayName;
                      _srcPlace = p;
                      _srcSug = [];
                      _srcOpen = false;
                    }),
                    onFocusLost: () =>
                        Future.delayed(const Duration(milliseconds: 150), () {
                      if (mounted) setState(() => _srcOpen = false);
                    }),
                  ),
                  // Connector
                  Padding(
                    padding: const EdgeInsets.only(left: 22),
                    child: Container(
                      height: 20,
                      width: 1,
                      color: Colors.white.withOpacity(0.15),
                    ),
                  ),
                  // TO field
                  _LocationField(
                    label: 'TO',
                    controller: _dstCtrl,
                    suggestions: _dstSug,
                    showDropdown: _dstOpen,
                    placeholder: 'Destination city',
                    dotColor: _dotDst,
                    onChanged: (v) => _debounceSearch(
                      value: v,
                      existing: _dstTimer,
                      save: (t) => _dstTimer = t,
                      onResult: (s) => _dstSug = s,
                      setOpen: (b) => _dstOpen = b,
                    ),
                    onSelect: (p) => setState(() {
                      _dstCtrl.text = p.displayName;
                      _dstPlace = p;
                      _dstSug = [];
                      _dstOpen = false;
                    }),
                    onFocusLost: () =>
                        Future.delayed(const Duration(milliseconds: 150), () {
                      if (mounted) setState(() => _dstOpen = false);
                    }),
                  ),
                  const SizedBox(height: 16),
                  // Speed
                  Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      _FieldLabel('AVG SPEED'),
                      const SizedBox(height: 6),
                      Row(
                        children: [
                          Expanded(
                            child: TextField(
                              controller: _speedCtrl,
                              keyboardType: const TextInputType.numberWithOptions(
                                  decimal: true),
                              inputFormatters: [
                                FilteringTextInputFormatter.allow(
                                    RegExp(r'[\d.]'))
                              ],
                              style: const TextStyle(
                                  color: _textPrimary, fontSize: 16),
                              decoration: _inputDecoration('55'),
                              onChanged: (_) => setState(() {}),
                            ),
                          ),
                          const SizedBox(width: 8),
                          Text('km/h',
                              style: TextStyle(
                                  color: Colors.white.withOpacity(0.5),
                                  fontSize: 14)),
                        ],
                      ),
                    ],
                  ),
                  const SizedBox(height: 20),
                  _ScanButton(
                    label: 'Scan My Route',
                    enabled: canScan,
                    loading: false,
                    onPressed: _handleScan,
                  ),
                ],
              ),
            ),
            if (_error != null) ...[
              const SizedBox(height: 12),
              _ErrorCard(_error!),
            ],
          ],
        ),
      ),
    );
  }

  // ── Loading / Scanning ────────────────────────────────────────────────────

  Widget _buildLoading() => SafeArea(
        child: Center(
          child: Padding(
            padding: const EdgeInsets.all(32),
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                // Radar rings animation
                SizedBox(
                  width: 80,
                  height: 80,
                  child: Stack(
                    alignment: Alignment.center,
                    children: [
                      _RadarRing(size: 80, opacity: 0.15),
                      _RadarRing(size: 56, opacity: 0.25),
                      _RadarRing(size: 32, opacity: 0.5),
                      Container(
                        width: 10,
                        height: 10,
                        decoration: const BoxDecoration(
                          color: _accent,
                          shape: BoxShape.circle,
                        ),
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 28),
                const Text(
                  'SCANNING RADAR',
                  style: TextStyle(
                    color: _textPrimary,
                    fontSize: 14,
                    letterSpacing: 2.5,
                    fontWeight: FontWeight.bold,
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  _state == _State.scanning && _scanStatus.isNotEmpty
                      ? _scanStatus
                      : 'Reading IMD frames · Mapping your route',
                  textAlign: TextAlign.center,
                  style: const TextStyle(color: _textSecondary, fontSize: 13),
                ),
                const SizedBox(height: 20),
                const SizedBox(
                  width: 40,
                  child: LinearProgressIndicator(
                    color: _accent,
                    backgroundColor: _input,
                  ),
                ),
              ],
            ),
          ),
        ),
      );

  // ── Results ───────────────────────────────────────────────────────────────

  Widget _buildResults() {
    final r = _result!;
    final wps = (r['waypoints'] as List?) ?? [];
    final hasRain = (r['rain_waypoints'] as int? ?? 0) > 0;
    final patches = _computePatches(wps);
    final lastEta = wps.isNotEmpty
        ? (wps.last['eta_mins'] as num? ?? 0).toDouble()
        : 0.0;
    final lagMins = r['radar_lag_mins'] as num?;
    final firstRainEta = r['first_rain_eta'] as num?;

    return SafeArea(
      child: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(16, 16, 16, 80),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Back + status pill
            Row(
              children: [
                GestureDetector(
                  onTap: _handleBack,
                  child: const Row(
                    children: [
                      Icon(Icons.arrow_back_ios,
                          size: 14, color: _textSecondary),
                      SizedBox(width: 4),
                      Text('Back',
                          style:
                              TextStyle(color: _textSecondary, fontSize: 14)),
                    ],
                  ),
                ),
                const Spacer(),
                _StatusPill(hasRain: hasRain),
              ],
            ),
            const SizedBox(height: 14),
            Text(_routeName,
                style: const TextStyle(
                    color: _textPrimary,
                    fontSize: 20,
                    fontWeight: FontWeight.bold)),
            const SizedBox(height: 12),
            // Rain banner
            _RainBanner(
              hasRain: hasRain,
              patches: patches,
              lastEta: lastEta,
              firstRainEta: firstRainEta?.toDouble(),
            ),
            const SizedBox(height: 12),
            // Stats
            Row(
              children: [
                Expanded(
                  child: _StatCard(
                    label: 'Distance',
                    value: _distanceKm != null
                        ? '${_distanceKm!.toStringAsFixed(1)} km'
                        : '—',
                  ),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: _StatCard(
                    label: hasRain
                        ? (firstRainEta != null && firstRainEta <= 2
                            ? 'Rain ends'
                            : 'Rain in')
                        : 'Rain',
                    value: hasRain && firstRainEta != null
                        ? '${firstRainEta.round()} min'
                        : 'None',
                  ),
                ),
              ],
            ),
            if (lagMins != null) ...[
              const SizedBox(height: 6),
              Center(
                child: Text(
                  'Radar: ~${lagMins.round()} min old',
                  style: TextStyle(
                      color: Colors.white.withOpacity(0.35), fontSize: 11),
                ),
              ),
            ],
            if (patches.isNotEmpty) ...[
              const SizedBox(height: 12),
              _TimelineCard(patches: patches, lastEta: lastEta),
            ],
            const SizedBox(height: 12),
            _WaypointsCard(waypoints: wps),
            const SizedBox(height: 12),
            _LegendCard(),
          ],
        ),
      ),
    );
  }
}

// ── Sub-widgets ───────────────────────────────────────────────────────────────

class _NavBar extends StatelessWidget {
  @override
  Widget build(BuildContext context) => Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          const Text('GARAJ BARAS',
              style: TextStyle(
                  color: _textPrimary,
                  fontSize: 15,
                  fontWeight: FontWeight.bold,
                  letterSpacing: 2)),
          Row(children: [
            Container(
                width: 6,
                height: 6,
                decoration: const BoxDecoration(
                    color: _green, shape: BoxShape.circle)),
            const SizedBox(width: 5),
            const Text('LIVE',
                style: TextStyle(
                    color: _green, fontSize: 11, letterSpacing: 1.2)),
          ]),
        ],
      );
}

class _FieldLabel extends StatelessWidget {
  final String text;
  const _FieldLabel(this.text);
  @override
  Widget build(BuildContext context) => Text(
        text,
        style: TextStyle(
          color: Colors.white.withOpacity(0.5),
          fontSize: 11,
          letterSpacing: 1.2,
          fontWeight: FontWeight.w600,
        ),
      );
}

InputDecoration _inputDecoration(String hint) => InputDecoration(
      hintText: hint,
      hintStyle: TextStyle(color: Colors.white.withOpacity(0.28)),
      filled: true,
      fillColor: _input,
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(10),
        borderSide: BorderSide.none,
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(10),
        borderSide: const BorderSide(color: _accent, width: 1.2),
      ),
      contentPadding:
          const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
    );

class _LocationField extends StatelessWidget {
  final String label, placeholder;
  final Color dotColor;
  final TextEditingController controller;
  final List<PlaceSuggestion> suggestions;
  final bool showDropdown;
  final ValueChanged<String> onChanged;
  final ValueChanged<PlaceSuggestion> onSelect;
  final VoidCallback onFocusLost;

  const _LocationField({
    required this.label,
    required this.placeholder,
    required this.dotColor,
    required this.controller,
    required this.suggestions,
    required this.showDropdown,
    required this.onChanged,
    required this.onSelect,
    required this.onFocusLost,
  });

  @override
  Widget build(BuildContext context) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Padding(
                padding: const EdgeInsets.only(top: 28),
                child: Container(
                  width: 10,
                  height: 10,
                  decoration:
                      BoxDecoration(color: dotColor, shape: BoxShape.circle),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    _FieldLabel(label),
                    const SizedBox(height: 5),
                    TextField(
                      controller: controller,
                      style: const TextStyle(
                          color: _textPrimary, fontSize: 15),
                      decoration: _inputDecoration(placeholder),
                      onChanged: onChanged,
                      onTapOutside: (_) => onFocusLost(),
                    ),
                  ],
                ),
              ),
            ],
          ),
          if (showDropdown && suggestions.isNotEmpty)
            Container(
              margin: const EdgeInsets.only(left: 22, top: 3),
              decoration: BoxDecoration(
                color: _input,
                borderRadius: BorderRadius.circular(10),
                border:
                    Border.all(color: Colors.white.withOpacity(0.1)),
              ),
              child: Column(
                children: suggestions
                    .map(
                      (s) => InkWell(
                        onTap: () => onSelect(s),
                        child: Padding(
                          padding: const EdgeInsets.symmetric(
                              horizontal: 14, vertical: 10),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(s.displayName,
                                  style: const TextStyle(
                                      color: _textPrimary, fontSize: 13)),
                              if (s.type.isNotEmpty)
                                Text(s.type,
                                    style: const TextStyle(
                                        color: _textSecondary,
                                        fontSize: 11)),
                            ],
                          ),
                        ),
                      ),
                    )
                    .toList(),
              ),
            ),
        ],
      );
}

class _ScanButton extends StatelessWidget {
  final String label;
  final bool enabled, loading;
  final VoidCallback onPressed;

  const _ScanButton({
    required this.label,
    required this.enabled,
    required this.loading,
    required this.onPressed,
  });

  @override
  Widget build(BuildContext context) => SizedBox(
        width: double.infinity,
        child: ElevatedButton(
          onPressed: enabled && !loading ? onPressed : null,
          style: ElevatedButton.styleFrom(
            backgroundColor: _accent,
            foregroundColor: _bg,
            disabledBackgroundColor: _input,
            disabledForegroundColor: _textSecondary,
            padding: const EdgeInsets.symmetric(vertical: 16),
            shape:
                RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
          ),
          child: loading
              ? const SizedBox(
                  width: 18,
                  height: 18,
                  child:
                      CircularProgressIndicator(strokeWidth: 2, color: _bg),
                )
              : Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Text(label,
                        style: const TextStyle(
                            fontWeight: FontWeight.bold, fontSize: 16)),
                    const SizedBox(width: 8),
                    const Icon(Icons.arrow_forward, size: 18),
                  ],
                ),
        ),
      );
}

class _StatusPill extends StatelessWidget {
  final bool hasRain;
  const _StatusPill({required this.hasRain});

  @override
  Widget build(BuildContext context) => Container(
        padding:
            const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        decoration: BoxDecoration(
          color: hasRain
              ? const Color(0xFF0A1520)
              : const Color(0xFF0A2010),
          borderRadius: BorderRadius.circular(20),
          border: Border.all(
              color: hasRain ? _accent : _green, width: 1),
        ),
        child: Text(
          hasRain ? 'Rain ahead' : 'Clear skies',
          style: TextStyle(
              color: hasRain ? _accent : _green,
              fontSize: 12,
              fontWeight: FontWeight.w600),
        ),
      );
}

class _RainBanner extends StatelessWidget {
  final bool hasRain;
  final List<_Patch> patches;
  final double lastEta;
  final double? firstRainEta;

  const _RainBanner({
    required this.hasRain,
    required this.patches,
    required this.lastEta,
    required this.firstRainEta,
  });

  @override
  Widget build(BuildContext context) {
    final Color bg, border;
    String headline;
    String? secondary;

    if (!hasRain) {
      bg = const Color(0xFF0A2010);
      border = _green.withOpacity(0.35);
      headline = 'No rain on route';
      secondary = 'Clear skies expected all the way.';
    } else if (patches.isNotEmpty) {
      final p = patches.first;
      final isNow = p.startMin <= 2;
      final continuesToEnd = p.endMin >= lastEta - 2.5;
      final isDying =
          p.decayStatus == 'dying' || p.decayStatus == 'dead';
      bg = const Color(0xFF0A1520);
      border = _accent.withOpacity(0.35);

      if (isDying) {
        headline = isNow
            ? "Rain nearby — but it's fading fast"
            : "Rain detected — likely to clear";
        secondary = 'This patch is losing intensity.';
      } else if (isNow && continuesToEnd) {
        headline = 'Rain right now — continues to destination';
        secondary = 'Expect rain for the full trip.';
      } else if (isNow) {
        headline =
            'Rain right now — clearing in ${p.endMin.round()} min';
        secondary = 'Skies clear after that.';
      } else if (continuesToEnd) {
        headline = 'Rain starts in ${p.startMin.round()} min';
        secondary =
            'Once it starts, rain continues to your destination.';
      } else {
        final dur = (p.endMin - p.startMin).round();
        headline =
            'Rain in ${p.startMin.round()} min, for ~$dur min';
        secondary = null;
      }
    } else {
      bg = const Color(0xFF0A1520);
      border = _accent.withOpacity(0.35);
      headline = 'Rain expected on route';
      secondary = null;
    }

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: border),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(headline,
              style: const TextStyle(
                  color: _textPrimary,
                  fontSize: 15,
                  fontWeight: FontWeight.w600)),
          if (secondary != null) ...[
            const SizedBox(height: 4),
            Text(secondary,
                style: TextStyle(
                    color: Colors.white.withOpacity(0.6), fontSize: 13)),
          ],
        ],
      ),
    );
  }
}

class _StatCard extends StatelessWidget {
  final String label, value;
  const _StatCard({required this.label, required this.value});

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: _card,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: Colors.white.withOpacity(0.06)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(label,
                style: TextStyle(
                    color: Colors.white.withOpacity(0.5), fontSize: 11)),
            const SizedBox(height: 4),
            Text(value,
                style: const TextStyle(
                    color: _textPrimary,
                    fontSize: 18,
                    fontWeight: FontWeight.bold)),
          ],
        ),
      );
}

class _TimelineCard extends StatefulWidget {
  final List<_Patch> patches;
  final double lastEta;
  const _TimelineCard({required this.patches, required this.lastEta});

  @override
  State<_TimelineCard> createState() => _TimelineCardState();
}

class _TimelineCardState extends State<_TimelineCard> {
  bool _expanded = false;

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: _card,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: Colors.white.withOpacity(0.06)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Rain Timeline',
                style: TextStyle(
                    color: Colors.white.withOpacity(0.55),
                    fontSize: 11,
                    letterSpacing: 1.0)),
            const SizedBox(height: 10),
            // Bar
            ClipRRect(
              borderRadius: BorderRadius.circular(4),
              child: SizedBox(
                height: 8,
                child: LayoutBuilder(builder: (ctx, c) {
                  return Stack(
                    children: [
                      Container(color: Colors.white.withOpacity(0.1)),
                      ...widget.patches.map((p) {
                        final left = widget.lastEta > 0
                            ? (p.startMin / widget.lastEta * c.maxWidth)
                                .clamp(0.0, c.maxWidth)
                            : 0.0;
                        final w = widget.lastEta > 0
                            ? ((p.endMin - p.startMin) /
                                    widget.lastEta *
                                    c.maxWidth)
                                .clamp(4.0, c.maxWidth - left)
                            : 4.0;
                        return Positioned(
                          left: left,
                          width: w,
                          top: 0,
                          bottom: 0,
                          child: Container(color: _patchColor(p.intensity)),
                        );
                      }),
                    ],
                  );
                }),
              ),
            ),
            const SizedBox(height: 8),
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text(_toIST(0),
                    style: const TextStyle(
                        color: _textSecondary, fontSize: 11)),
                Text(_toIST(widget.lastEta),
                    style: const TextStyle(
                        color: _textSecondary, fontSize: 11)),
              ],
            ),
            const SizedBox(height: 8),
            GestureDetector(
              onTap: () => setState(() => _expanded = !_expanded),
              child: Text(
                _expanded
                    ? 'Hide breakdown'
                    : 'See breakdown (${widget.patches.length} rain zone${widget.patches.length == 1 ? '' : 's'})',
                style: const TextStyle(color: _accent, fontSize: 13),
              ),
            ),
            if (_expanded) ...[
              const SizedBox(height: 8),
              ...widget.patches.map((p) => Padding(
                    padding: const EdgeInsets.symmetric(vertical: 5),
                    child: Row(
                      children: [
                        Container(
                          width: 10,
                          height: 10,
                          decoration: BoxDecoration(
                            color: _patchColor(p.intensity),
                            shape: BoxShape.circle,
                          ),
                        ),
                        const SizedBox(width: 10),
                        Expanded(
                          child: Text(
                            '${_toIST(p.startMin)} – ${_toIST(p.endMin)}  ·  ${p.intensity}',
                            style: const TextStyle(
                                color: _textPrimary, fontSize: 13),
                          ),
                        ),
                        if (p.decayStatus == 'dying' ||
                            p.decayStatus == 'dead')
                          _DecayChip('Fading'),
                        if (p.decayStatus == 'weakening')
                          _DecayChip('Weakening'),
                      ],
                    ),
                  )),
            ],
          ],
        ),
      );
}

class _DecayChip extends StatelessWidget {
  final String label;
  const _DecayChip(this.label);
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
        decoration: BoxDecoration(
          color: const Color(0xFF7C3AED).withOpacity(0.2),
          borderRadius: BorderRadius.circular(4),
          border: Border.all(
              color: const Color(0xFF7C3AED).withOpacity(0.4)),
        ),
        child: Text(label,
            style: const TextStyle(
                color: Color(0xFFA78BFA), fontSize: 10)),
      );
}

class _WaypointsCard extends StatelessWidget {
  final List<dynamic> waypoints;
  const _WaypointsCard({required this.waypoints});

  @override
  Widget build(BuildContext context) {
    final rain =
        waypoints.where((w) => (w as Map)['rain_expected'] == true).toList();
    if (rain.isEmpty) return const SizedBox.shrink();
    final shown = rain.take(8).toList();

    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: _card,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: Colors.white.withOpacity(0.06)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Rain Waypoints (${rain.length})',
              style: TextStyle(
                  color: Colors.white.withOpacity(0.55),
                  fontSize: 11,
                  letterSpacing: 1.0)),
          const SizedBox(height: 8),
          ...shown.map((w) {
            final wp = w as Map;
            final eta = (wp['eta_mins'] as num? ?? 0).toDouble();
            final label = wp['label']?.toString() ?? '';
            final decay = wp['decay_status']?.toString();
            return Padding(
              padding: const EdgeInsets.symmetric(vertical: 4),
              child: Row(
                children: [
                  Container(
                    width: 8,
                    height: 8,
                    decoration: BoxDecoration(
                        color: _rainColor(label),
                        shape: BoxShape.circle),
                  ),
                  const SizedBox(width: 10),
                  Text(_toIST(eta),
                      style: const TextStyle(
                          color: _textSecondary, fontSize: 12)),
                  const SizedBox(width: 8),
                  Expanded(
                      child: Text(label,
                          style: const TextStyle(
                              color: _textPrimary, fontSize: 13))),
                  if (decay != null && decay != 'stable')
                    Text(
                      decay == 'dead'
                          ? 'Clearing'
                          : decay == 'dying'
                              ? 'Fading'
                              : 'Weakening',
                      style: const TextStyle(
                          color: Color(0xFFA78BFA), fontSize: 11),
                    ),
                ],
              ),
            );
          }),
          if (rain.length > 8) ...[
            const SizedBox(height: 4),
            Text('+${rain.length - 8} more',
                style: TextStyle(
                    color: Colors.white.withOpacity(0.35), fontSize: 11)),
          ],
        ],
      ),
    );
  }
}

class _LegendCard extends StatelessWidget {
  const _LegendCard();

  static const _entries = [
    ('Very Heavy', Color(0xFFEF4444)),
    ('Heavy', Color(0xFFF59E0B)),
    ('Moderate', Color(0xFF0EA5E9)),
    ('Light', Color(0xFF38BDF8)),
    ('Very Light', Color(0xFF7DD3FC)),
    ('No Rain', Colors.white),
  ];

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: _card,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: Colors.white.withOpacity(0.06)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Rain Intensity',
                style: TextStyle(
                    color: Colors.white.withOpacity(0.55),
                    fontSize: 11,
                    letterSpacing: 1.0)),
            const SizedBox(height: 10),
            Wrap(
              spacing: 12,
              runSpacing: 8,
              children: _entries
                  .map((e) => Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Container(
                            width: 10,
                            height: 10,
                            decoration: BoxDecoration(
                                color: e.$2, shape: BoxShape.circle),
                          ),
                          const SizedBox(width: 5),
                          Text(e.$1,
                              style: const TextStyle(
                                  color: _textSecondary, fontSize: 12)),
                        ],
                      ))
                  .toList(),
            ),
          ],
        ),
      );
}

class _RadarRing extends StatelessWidget {
  final double size, opacity;
  const _RadarRing({required this.size, required this.opacity});

  @override
  Widget build(BuildContext context) => Container(
        width: size,
        height: size,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          border: Border.all(
              color: _accent.withOpacity(opacity), width: 1.5),
        ),
      );
}
