# DreamScreen Bridge

Porta i colori del DreamScreen 4K sulle luci WLED senza modificare il firmware: il bridge
si presenta al DreamScreen come una SideKick, riceve i 12 settori di colore (~25 fps) e li
inoltra a WLED via DDP. Funziona in modalità Video e Musica.

## Installazione su Home Assistant OS

1. Impostazioni → Componenti aggiuntivi → Store → ⋮ → **Repository** → aggiungi `https://github.com/IlGiurgi/dreamscreen-bridge`.
2. Nello Store compare la sezione **DreamScreen Bridge** (se no: ⋮ → **Verifica aggiornamenti**):
   apri l'add-on e installalo.
3. Scheda **Configurazione** per luci e impostazioni, poi **Avvia**. Controlla il **Registro**:
   deve comparire `Streaming`.
4. (Facoltativo) interruttore in HA: copia `homeassistant/dreamscreen_sync.yaml` in
   `/config/packages/` e verifica che `configuration.yaml` contenga
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
   poi riavvia HA. Compare `input_boolean.dreamscreen_sync` che avvia/ferma l'add-on.

## Impostazioni

| Opzione | Significato |
|---|---|
| `dreamscreen_ip` | vuoto = ricerca automatica |
| `group` | il DreamScreen trasmette solo alle SideKick del suo gruppo; se è nel gruppo 0 viene spostato qui |
| `gain` | intensità colori (1 = originale) |
| `smoothing` | morbidezza transizioni (0–0.95) |
| `devices` | luci: `name`, `ip`, `leds`, `mode`, `sectors`, `reverse`, `protocol` (`ddp` per WLED, `drgb` per l'effetto WLED di ESPHome) |

Modalità per luce:
- `average`: colore unico, media dei `sectors` (es. `"9,10,11,12,1"` = bordo inferiore)
- `spread`: lo schermo da sinistra a destra lungo la striscia
- `center`: dal centro dello schermo (centro striscia) verso i lati (estremi)

Settori come li vedi dal divano:

```
 7   6   5   4   3
 8               2
 9  10  11  12   1
```

## Prova dal PC

```bash
python dreamscreen_bridge/bridge.py            # sync con le impostazioni predefinite
python dreamscreen_bridge/bridge.py --test 10  # arcobaleno finto per 10 s
```
