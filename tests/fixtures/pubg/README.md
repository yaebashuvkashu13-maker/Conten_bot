# PUBG media integration fixtures

Place short real clips here (binaries are not committed by default).

Expected layout used by `tests/test_pubg_media_integration.py`:

| File | Intent |
|------|--------|
| `kill_confirmed.mp4` | Full fight ending with author kill notification |
| `shooting_no_kill.mp4` | Gunfire without kill / knock payoff |
| `loot_walk.mp4` | Loot / walk, no fight |
| `author_death.mp4` | Author dies without kill |
| `movable_blue_kill.mp4` | Kill text not in default HUD slot |
| `blue_hud_false_positive.mp4` | Map/UI blue that is not a kill toast |
| `odd_viewport.mp4` | Phone hands / donation ticker / vertical restream |

Override directory with `PUBG_MEDIA_FIXTURES_DIR`. Tests skip when files are absent.
