# 🐎 Buscador hípico de Chile

Al abrir la app, busca sola las próximas reuniones de **Hipódromo Chile**, **Club Hípico de
Santiago** y **Valparaíso Sporting** (vía teletrak.cl, que publica el link del volante de cada
uno sin depender de JavaScript). Nadie necesita subir ni pegar nada: solo escribir qué buscar
(caballo, criadero/haras, jinete, preparador o stud).

Si quieres revisar otra fecha, hay un panel desplegable para adjuntar un volante PDF (Hipódromo
Chile o Club Hípico, se detecta solo) o pegar el link de una reunión de Sporting.

## Publicarla (Streamlit Community Cloud)
Sube estos TRES archivos sueltos a la raíz de tu repositorio de GitHub (no una carpeta ni un zip):
`app.py`, `requirements.txt`, `packages.txt`.

Luego en https://share.streamlit.io → Create app → elige tu repositorio, rama `main`,
archivo `app.py` → Deploy.

Si ya la habías publicado antes, basta con reemplazar `app.py` en GitHub: la app se actualiza sola.

## Cómo funciona por dentro
- `teletrak.cl` (la plataforma de apuestas de los tres hipódromos) renderiza en HTML plano,
  sin JavaScript, la semana de carreras con el link al volante de cada uno. De ahí la app saca
  la fecha y el PDF de Hipódromo Chile, y la fecha de Sporting.
- El volante de Hipódromo Chile y del Club Hípico se leen en PDF (`pdftotext`/`pdfplumber`).
- Valparaíso Sporting se lee desde sus propias páginas HTML por carrera (`sporting.cl`), que sí
  traen el criador en el texto.

## Notas
* Los retiros de última hora no aparecen en los programas; confírmalos en el hipódromo.
* Si alguno de los tres sitios cambia de formato, la app lo mostrará como un aviso en vez de fallar.
