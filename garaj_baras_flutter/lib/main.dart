import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'route_screen.dart';
import 'nowcast_screen.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  SystemChrome.setPreferredOrientations([
    DeviceOrientation.portraitUp,
    DeviceOrientation.portraitDown,
  ]);
  runApp(const GarajBarasApp());
}

class GarajBarasApp extends StatelessWidget {
  const GarajBarasApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Garaj Baras',
      debugShowCheckedModeBanner: false,
      theme: _buildTheme(),
      home: const _MainShell(),
    );
  }

  ThemeData _buildTheme() {
    const bg = Color(0xFF05101F);
    const accent = Color(0xFF38BDF8);

    return ThemeData(
      brightness: Brightness.dark,
      scaffoldBackgroundColor: bg,
      colorScheme: const ColorScheme.dark(
        primary: accent,
        secondary: accent,
        surface: Color(0xFF0D1B2E),
      ),
      textTheme: const TextTheme(
        bodyMedium: TextStyle(color: Colors.white),
        bodySmall: TextStyle(color: Color(0xFF94A3B8)),
      ),
      inputDecorationTheme: const InputDecorationTheme(
        filled: true,
        fillColor: Color(0xFF1A2840),
        border: OutlineInputBorder(borderSide: BorderSide.none),
      ),
      useMaterial3: true,
    );
  }
}

class _MainShell extends StatefulWidget {
  const _MainShell();

  @override
  State<_MainShell> createState() => _MainShellState();
}

class _MainShellState extends State<_MainShell> {
  int _idx = 0;

  @override
  Widget build(BuildContext context) {
    // Keep status bar icons light on dark background
    SystemChrome.setSystemUIOverlayStyle(const SystemUiOverlayStyle(
      statusBarColor: Colors.transparent,
      statusBarIconBrightness: Brightness.light,
    ));

    return Scaffold(
      backgroundColor: const Color(0xFF05101F),
      body: IndexedStack(
        index: _idx,
        children: const [
          RouteScreen(),
          NowcastScreen(),
        ],
      ),
      bottomNavigationBar: NavigationBar(
        backgroundColor: const Color(0xFF0D1B2E),
        indicatorColor: const Color(0xFF1A2840),
        surfaceTintColor: Colors.transparent,
        shadowColor: Colors.black,
        elevation: 8,
        selectedIndex: _idx,
        onDestinationSelected: (i) => setState(() => _idx = i),
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.directions_car_outlined, color: Color(0xFF94A3B8)),
            selectedIcon: Icon(Icons.directions_car, color: Color(0xFF38BDF8)),
            label: 'Route',
          ),
          NavigationDestination(
            icon: Icon(Icons.radar_outlined, color: Color(0xFF94A3B8)),
            selectedIcon: Icon(Icons.radar, color: Color(0xFF38BDF8)),
            label: 'Nowcast',
          ),
        ],
      ),
    );
  }
}
