[out:json][timeout:45];
(
  nwr["amenity"="cafe"][~"^name(:ru|:kk|:en)?$"~"."](51.0,71.2,51.32,71.7);
  nwr["amenity"="restaurant"][~"^name(:ru|:kk|:en)?$"~"."](51.0,71.2,51.32,71.7);
  nwr["amenity"="bar"][~"^name(:ru|:kk|:en)?$"~"."](51.0,71.2,51.32,71.7);
  nwr["amenity"="pub"][~"^name(:ru|:kk|:en)?$"~"."](51.0,71.2,51.32,71.7);
  nwr["amenity"="theatre"][~"^name(:ru|:kk|:en)?$"~"."](51.0,71.2,51.32,71.7);
  nwr["leisure"="park"](51.0,71.2,51.32,71.7);
  nwr["leisure"="garden"](51.0,71.2,51.32,71.7);
  nwr["leisure"="playground"](51.0,71.2,51.32,71.7);
  nwr["tourism"="museum"][~"^name(:ru|:kk|:en)?$"~"."](51.0,71.2,51.32,71.7);
  nwr["tourism"="attraction"][~"^name(:ru|:kk|:en)?$"~"."](51.0,71.2,51.32,71.7);
  nwr["tourism"="viewpoint"][~"^name(:ru|:kk|:en)?$"~"."](51.0,71.2,51.32,71.7);
  nwr["historic"="monument"][~"^name(:ru|:kk|:en)?$"~"."](51.0,71.2,51.32,71.7);
  nwr["historic"="memorial"][~"^name(:ru|:kk|:en)?$"~"."](51.0,71.2,51.32,71.7);
  nwr["shop"="mall"][~"^name(:ru|:kk|:en)?$"~"."](51.0,71.2,51.32,71.7);
);
out center tags meta;
