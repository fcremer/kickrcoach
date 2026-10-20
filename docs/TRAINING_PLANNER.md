# Trainingsplanung mit dem bestehenden ChatGPT-Chat

Ein Trainingsplanungs-Chat kann vorhandene Trainingsinformationen strukturieren,
Wochen planen und YAML-Workouts ausgeben. Er ist keine Laufzeitkomponente des
Trainers und erhält weder automatischen Archivzugriff noch BLE-Steuerung.

## Kontext übernehmen

Den Arbeitsauftrag im bestehenden Chat verwenden, in dem die Historie liegt.
Zuerst ein datiertes Athletenprofil und eine Chronik mit Quellen, Annahmen,
Widersprüchen und offenen Punkten erstellen lassen. Tatsächlich absolvierte
Einheiten müssen von Planvorschlägen getrennt werden. Eine kurze technische
App-Probe ist kein FTP-Test und keine individuelle Trainingsfreigabe.

Eine separat gesicherte Zusammenfassung macht den Kontext übertragbar. Bei
wichtigen Detailfragen gehören Originaldaten dazu; der Planer darf nicht
behaupten, den vollständigen Verlauf zu kennen, wenn ihm ältere Inhalte fehlen.
Persönliche Profile, Zusammenfassungen und Chat-Exporte gehören in den lokal
ignorierten Ordner `training_context/`, nicht in öffentliche Commits.

Für längerfristige Nutzung können ChatGPT-Projekte zugehörige Chats, Dateien
und Projektanweisungen bündeln. Benötigte Quellen müssen dort bereitgestellt
oder verbunden werden; ein ChatGPT-Projekt allein öffnet keinen lokalen Ordner.
Siehe [offizielle OpenAI-Dokumentation zu Projekten](https://learn.chatgpt.com/docs/projects).

## Von Planung zu tatsächlicher Durchführung

1. Nächste Woche auf bestätigte Historie, Ziel, Zeitbudget und aktuelle Erholung
   stützen. Fehlende Indoor-Basis nicht durch eine erfundene FTP ersetzen.
2. Jede Einheit als eigene YAML-Datei ausgeben. Die Blöcke müssen absolute
   Wattziele und Sekundendauern enthalten, mit erstem Ziel <=50 W. Optionale
   Kadenzvorgaben stehen pro Block unter `cadence.minimum_rpm` / `maximum_rpm`.
3. Datei lokal in `workouts/` speichern, validieren und nach Serverneustart in
   der GUI auswählen. Texte zu RPE sind manuelle Hinweise, keine Programmzweige.
4. Nach der Fahrt RPE/Kühlung/Notizen ergänzen. JSON selbst im Planungs-Chat
   bereitstellen; die App sendet nichts automatisch.
5. Datenqualität, Pausen, Abbrüche und manuelle Änderungen vor einer Planänderung
   berücksichtigen. Profil/Chronik anschließend anhand bestätigter Fakten
   aktualisieren und separat sichern.

In diesem Arbeitsablauf wird kein eigener GPT im Nutzerkonto automatisch
angelegt. Ein wiederverwendbarer Arbeitsauftrag wird im gewählten Chat
verwendet. Die Kontoeinrichtung bleibt in der ChatGPT-Oberfläche.
