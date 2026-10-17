"""Экспорт результатов анализа (ARCHITECTURE §17): полный отчёт PDF/DOCX, список рисков CSV/XLSX.

Весь пользовательский и модельный текст считается недоверенным: в PDF экранируется разметка
reportlab, в DOCX пишется только как текст, в CSV/XLSX защищён от formula injection
(`app.exports.safety`). Секреты в отчёт не попадают: из snapshot берутся только перечисленные поля.
"""

EXPORT_VERSION = "export-1"
