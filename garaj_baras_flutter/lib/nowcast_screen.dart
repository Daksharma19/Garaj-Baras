import 'dart:async';
import 'package:flutter/material.dart';
import 'api.dart';

// ── Colours (same palette as route_screen) ────────────────────────────────────
const _bg = Color(0xFF05101F);
const _card = Color(0xFF0D1B2E);
const _input = Color(0xFF1A2840);
const _accent = Color(0xFF38BDF8);
const _textPrimary = Colors.white;
const _textSecondary = Color(0xFF94A3B8);
const _green = Color(0xFF22C55E);

String _toIST(double etaMins) {
  final t = DateTime.now()
      .toUtc()
      .add(const Duration(hours: 5, minutes: 30))
      .add(Duration(minutes: etaMins.round()));
  return '${t.hour.toString().padLeft(2, '0')}:${t.minute.toString().padLeft(2, '0')}';
}

// ── Screen ────────────────────────────────────────────────────────────────────

class NowcastScreen extends StatefulWidget {
  const NowcastScreen({super.key});

  @override
  State<NowcastScreen> createState() => _NowcastScreenState();
}

class _NowcastScreenState extends State<NowcastScreen> {
  final _searchCtrl = TextEditingController();
  double? _lat, _lon;
  String _locName = '';

  List<PlaceSuggestion> _suggestions = [];
  bool _sugOpen = false;
  Timer? _debounce;

  bool _loading = false;
  String _scanStatus = '';
  String? _error;
  Map<String, dynamic>? _result;

  @override
  void dispose() {
    _searchCtrl.dispose();
    _debounce?.cancel();
    super.dispose();
  }

  void _onSearchChanged(String value) {
    _debounce?.cancel();
    if (value.trim().length < 2) {
      setState(() {
        _suggestions = [];
        _sugOpen = false;
      });
      return;
    }
    _debounce = Timer(const Duration(milliseconds: 350), () async {
      final sug = await ApiService.searchPlaces(value);
      if (mounted) {
        setState(() {
          _suggestions = sug;
          _sugOpen = sug.isNotEmpty;
        });
      }
    });
  }

  void _selectPlace(PlaceSuggestion p) {
    setState(() {
      _lat = p.lat;
      _lon = p.lon;
      _locName = p.shortName;
      _searchCtrl.clear();
      _suggestions = [];
      _sugOpen = false;
      _result = null;
      _error = null;
    });
  }

  Future<void> _handleScan() async {
    if (_lat == null || _lon == null) {
      setState(() => _error = 'Please select a location first.');
      return;
    }
    setState(() {
      _loading = true;
      _error = null;
      _result = null;
      _scanStatus = 'Scanning radar…';
    });
    try {
      final result = await ApiService.nowcast(_lat!, _lon!);
      if (!mounted) return;
      setState(() {
        _result = result;
        _loading = false;
        _scanStatus = '';
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _scanStatus = '';
        _error = e.toString().replaceAll('Exception: ', '');
      });
    }
  }

  @override
  Widget build(BuildContext context) => SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(16, 16, 16, 80),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Nav
              Row(
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
              ),
              const SizedBox(height: 20),

              // Scan card
              Container(
                padding: const EdgeInsets.all(20),
                decoration: BoxDecoration(
                  color: _card,
                  borderRadius: BorderRadius.circular(16),
                  border:
                      Border.all(color: Colors.white.withOpacity(0.06)),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('SCAN LOCATION',
                        style: TextStyle(
                            color: Colors.white.withOpacity(0.5),
                            fontSize: 11,
                            letterSpacing: 1.2,
                            fontWeight: FontWeight.w600)),
                    const SizedBox(height: 12),

                    // Selected location display
                    if (_lat != null) ...[
                      Container(
                        padding: const EdgeInsets.all(12),
                        decoration: BoxDecoration(
                          color: _input,
                          borderRadius: BorderRadius.circular(10),
                        ),
                        child: Row(
                          children: [
                            const Icon(Icons.location_on,
                                color: _accent, size: 18),
                            const SizedBox(width: 10),
                            Expanded(
                              child: Column(
                                crossAxisAlignment:
                                    CrossAxisAlignment.start,
                                children: [
                                  Text(
                                    _locName.isNotEmpty
                                        ? _locName
                                        : 'Selected location',
                                    style: const TextStyle(
                                        color: _textPrimary,
                                        fontSize: 14,
                                        fontWeight: FontWeight.w500),
                                  ),
                                  Text(
                                    '${_lat!.toStringAsFixed(4)}°N, ${_lon!.toStringAsFixed(4)}°E',
                                    style: const TextStyle(
                                        color: _textSecondary, fontSize: 11),
                                  ),
                                ],
                              ),
                            ),
                          ],
                        ),
                      ),
                      const SizedBox(height: 10),
                    ],

                    // Search field
                    TextField(
                      controller: _searchCtrl,
                      style:
                          const TextStyle(color: _textPrimary, fontSize: 15),
                      decoration: InputDecoration(
                        hintText: _lat != null
                            ? 'Search a different location…'
                            : 'Search city or area…',
                        hintStyle: TextStyle(
                            color: Colors.white.withOpacity(0.28)),
                        prefixIcon: const Icon(Icons.search,
                            color: _textSecondary, size: 20),
                        filled: true,
                        fillColor: _input,
                        border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(10),
                          borderSide: BorderSide.none,
                        ),
                        focusedBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(10),
                          borderSide:
                              const BorderSide(color: _accent, width: 1.2),
                        ),
                        contentPadding: const EdgeInsets.symmetric(
                            horizontal: 14, vertical: 12),
                      ),
                      onChanged: _onSearchChanged,
                      onTapOutside: (_) {
                        Future.delayed(
                            const Duration(milliseconds: 150), () {
                          if (mounted) setState(() => _sugOpen = false);
                        });
                      },
                    ),

                    // Suggestions dropdown
                    if (_sugOpen && _suggestions.isNotEmpty)
                      Container(
                        margin: const EdgeInsets.only(top: 4),
                        decoration: BoxDecoration(
                          color: _input,
                          borderRadius: BorderRadius.circular(10),
                          border: Border.all(
                              color: Colors.white.withOpacity(0.1)),
                        ),
                        child: Column(
                          children: _suggestions
                              .map((s) => InkWell(
                                    onTap: () => _selectPlace(s),
                                    child: Padding(
                                      padding: const EdgeInsets.symmetric(
                                          horizontal: 14, vertical: 10),
                                      child: Row(
                                        children: [
                                          const Icon(
                                              Icons.location_on_outlined,
                                              size: 14,
                                              color: _textSecondary),
                                          const SizedBox(width: 8),
                                          Expanded(
                                            child: Column(
                                              crossAxisAlignment:
                                                  CrossAxisAlignment.start,
                                              children: [
                                                Text(s.displayName,
                                                    style: const TextStyle(
                                                        color: _textPrimary,
                                                        fontSize: 13)),
                                                if (s.type.isNotEmpty)
                                                  Text(s.type,
                                                      style:
                                                          const TextStyle(
                                                              color:
                                                                  _textSecondary,
                                                              fontSize: 11)),
                                              ],
                                            ),
                                          ),
                                        ],
                                      ),
                                    ),
                                  ))
                              .toList(),
                        ),
                      ),

                    const SizedBox(height: 16),
                    _ScanBtn(
                      enabled: _lat != null,
                      loading: _loading,
                      status: _scanStatus,
                      onPressed: _handleScan,
                    ),
                  ],
                ),
              ),

              // Error
              if (_error != null) ...[
                const SizedBox(height: 12),
                _ErrorCard(_error!),
              ],

              // Results
              if (_result != null && !_loading) ...[
                const SizedBox(height: 16),
                _ResultsView(result: _result!),
              ],
            ],
          ),
        ),
      );
}

// ── Scan Button ───────────────────────────────────────────────────────────────

class _ScanBtn extends StatelessWidget {
  final bool enabled, loading;
  final String status;
  final VoidCallback onPressed;

  const _ScanBtn({
    required this.enabled,
    required this.loading,
    required this.status,
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
            shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(12)),
          ),
          child: loading
              ? Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(
                          strokeWidth: 2, color: _bg),
                    ),
                    const SizedBox(width: 8),
                    Text(status.isEmpty ? 'Scanning…' : status,
                        style: const TextStyle(
                            fontWeight: FontWeight.bold,
                            fontSize: 15,
                            color: _bg)),
                  ],
                )
              : const Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Text('Scan Next 2 Hours',
                        style: TextStyle(
                            fontWeight: FontWeight.bold, fontSize: 15)),
                    SizedBox(width: 8),
                    Icon(Icons.arrow_forward, size: 18),
                  ],
                ),
        ),
      );
}

// ── Results ───────────────────────────────────────────────────────────────────

class _ResultsView extends StatelessWidget {
  final Map<String, dynamic> result;
  const _ResultsView({required this.result});

  @override
  Widget build(BuildContext context) {
    final inBounds = result['in_radar_bounds'] as bool? ?? false;

    if (!inBounds) {
      return Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: const Color(0xFF1A1500),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(
              color: const Color(0xFFF59E0B).withOpacity(0.4)),
        ),
        child: const Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Outside radar coverage',
                style: TextStyle(
                    color: _textPrimary,
                    fontSize: 15,
                    fontWeight: FontWeight.w600)),
            SizedBox(height: 4),
            Text(
              'This location is outside IMD radar coverage.\nTry a location in Delhi NCR or Uttar Pradesh.',
              style: TextStyle(color: _textSecondary, fontSize: 13),
            ),
          ],
        ),
      );
    }

    final slots = (result['slots'] as List?)?.cast<Map<String, dynamic>>() ?? [];
    final summary = result['summary'] as String? ?? '';
    final hasRain = (result['rain_slots'] as int? ?? 0) > 0;
    final asOf = result['radar_as_of'] as String?;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        // Summary banner
        Container(
          width: double.infinity,
          padding: const EdgeInsets.all(16),
          decoration: BoxDecoration(
            color: hasRain
                ? const Color(0xFF0A1520)
                : const Color(0xFF0A2010),
            borderRadius: BorderRadius.circular(12),
            border: Border.all(
              color: (hasRain ? _accent : _green).withOpacity(0.4),
            ),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(summary,
                  style: const TextStyle(
                      color: _textPrimary,
                      fontSize: 15,
                      fontWeight: FontWeight.w600)),
              if (asOf != null) ...[
                const SizedBox(height: 4),
                Text('Radar as of $asOf',
                    style: const TextStyle(
                        color: _textSecondary, fontSize: 12)),
              ],
            ],
          ),
        ),
        const SizedBox(height: 12),
        // Slots
        if (slots.isNotEmpty) _SlotsCard(slots: slots),
      ],
    );
  }
}

// ── 2-hour Slot Cards ─────────────────────────────────────────────────────────

class _SlotsCard extends StatelessWidget {
  final List<Map<String, dynamic>> slots;
  const _SlotsCard({required this.slots});

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
            Text('Next 2 Hours',
                style: TextStyle(
                    color: Colors.white.withOpacity(0.55),
                    fontSize: 11,
                    letterSpacing: 1.0)),
            const SizedBox(height: 12),
            ...slots.asMap().entries.map((e) {
              final i = e.key;
              final s = e.value;
              final hasRain = s['has_rain'] as bool? ?? false;
              final prob = (s['probability'] as num? ?? 0).toInt();
              final slotMins = (s['slot_mins'] as num? ?? 0).toDouble();
              final intensity = s['intensity'] as String? ?? '';
              final decay = s['decay_status'] as String?;
              final filled = (prob / 10).round().clamp(0, 10);
              final isNow = i == 0;

              return Padding(
                padding: const EdgeInsets.symmetric(vertical: 5),
                child: Row(
                  children: [
                    // Time
                    SizedBox(
                      width: 42,
                      child: Text(
                        isNow ? 'Now' : _toIST(slotMins),
                        style: const TextStyle(
                            color: _textSecondary, fontSize: 12),
                      ),
                    ),
                    const SizedBox(width: 8),
                    // Rain bar (10 segments)
                    Expanded(
                      child: Row(
                        children: List.generate(
                          10,
                          (j) => Expanded(
                            child: Container(
                              height: 6,
                              margin: const EdgeInsets.symmetric(
                                  horizontal: 1),
                              decoration: BoxDecoration(
                                color: j < filled
                                    ? (hasRain
                                        ? _accent
                                        : _accent.withOpacity(0.25))
                                    : Colors.white.withOpacity(0.07),
                                borderRadius: BorderRadius.circular(2),
                              ),
                            ),
                          ),
                        ),
                      ),
                    ),
                    const SizedBox(width: 8),
                    // Label
                    SizedBox(
                      width: 60,
                      child: Text(
                        hasRain
                            ? (intensity.isNotEmpty ? intensity : 'Rain')
                            : 'No Rain',
                        style: TextStyle(
                          color: hasRain ? _textPrimary : _textSecondary,
                          fontSize: 12,
                        ),
                      ),
                    ),
                    // Probability
                    SizedBox(
                      width: 36,
                      child: Text(
                        hasRain ? '$prob%' : '—',
                        textAlign: TextAlign.right,
                        style: TextStyle(
                          color: hasRain ? _accent : _textSecondary,
                          fontSize: 12,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                    // Decay chip
                    if (hasRain &&
                        decay != null &&
                        decay != 'stable') ...[
                      const SizedBox(width: 6),
                      Container(
                        padding: const EdgeInsets.symmetric(
                            horizontal: 5, vertical: 2),
                        decoration: BoxDecoration(
                          color: const Color(0xFF7C3AED).withOpacity(0.15),
                          borderRadius: BorderRadius.circular(4),
                          border: Border.all(
                              color: const Color(0xFF7C3AED)
                                  .withOpacity(0.3)),
                        ),
                        child: Text(
                          switch (decay) {
                            'dying' => 'Fading',
                            'dead' => 'Clearing',
                            _ => 'Weakening',
                          },
                          style: const TextStyle(
                              color: Color(0xFFA78BFA), fontSize: 9),
                        ),
                      ),
                    ],
                  ],
                ),
              );
            }),
          ],
        ),
      );
}

// ── Shared small widgets ──────────────────────────────────────────────────────

class _ErrorCard extends StatelessWidget {
  final String message;
  const _ErrorCard(this.message);

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: const Color(0xFF2A0A0A),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(
              color: const Color(0xFFEF4444).withOpacity(0.4)),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('!',
                style: TextStyle(
                    color: Color(0xFFEF4444),
                    fontSize: 16,
                    fontWeight: FontWeight.bold)),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text('Scan failed',
                      style: TextStyle(
                          color: Color(0xFFFCA5A5),
                          fontWeight: FontWeight.w600,
                          fontSize: 13)),
                  const SizedBox(height: 2),
                  Text(message,
                      style: const TextStyle(
                          color: Color(0xFFFCA5A5), fontSize: 12)),
                ],
              ),
            ),
          ],
        ),
      );
}
